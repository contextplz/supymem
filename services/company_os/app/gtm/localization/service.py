"""Market Profile and GTM Thesis lifecycle.

Profiles distinguish the Supymem customer's business from its downstream
market. Theses are evidence-ranked, policy-checked, versioned and explicitly
approved before an execution worker may consume them.
"""

from __future__ import annotations

import json
import re
from typing import Any

from sqlalchemy import func, select

from app.db.models import GtmCampaignExperiment, GtmMarketProfile, GtmThesis
from app.gtm.localization.policy import EVIDENCE_CLASSES, assess_profile, assess_thesis
from app.state import db
from core.clock import utcnow
from core.ids import new_id


def _dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _load(value: str | None, default: Any) -> Any:
    try:
        return json.loads(value or "")
    except (json.JSONDecodeError, TypeError):
        return default


def _profile_dict(row: GtmMarketProfile) -> dict[str, Any]:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "version": row.version,
        "name": row.name,
        "status": row.status,
        "seller": _load(row.seller_json, {}),
        "product": _load(row.product_json, {}),
        "segment": _load(row.segment_json, {}),
        "constraints": _load(row.constraints_json, {}),
        "evidence": _load(row.evidence_json, []),
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _thesis_dict(row: GtmThesis) -> dict[str, Any]:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "profile_id": row.profile_id,
        "version": row.version,
        "status": row.status,
        "archetype": _load(row.archetype_json, {}),
        "buying_profile": _load(row.buying_profile_json, {}),
        "strategy": _load(row.strategy_json, {}),
        "assumptions": _load(row.assumptions_json, []),
        "evidence": _load(row.evidence_json, []),
        "policy": _load(row.policy_json, {}),
        "confidence": row.confidence,
        "model": row.model,
        "approved_by": row.approved_by,
        "approved_at": row.approved_at.isoformat() if row.approved_at else None,
        "created_at": row.created_at.isoformat(),
    }


def create_market_profile(
    workspace_id: str,
    *,
    name: str,
    seller: dict[str, Any],
    product: dict[str, Any],
    segment: dict[str, Any],
    constraints: dict[str, Any] | None = None,
    evidence: list[dict[str, Any]] | None = None,
    created_by: str | None = None,
) -> dict[str, Any]:
    payload = {
        "seller": seller or {},
        "product": product or {},
        "segment": segment or {},
        "constraints": constraints or {},
        "evidence": evidence or [],
    }
    policy = assess_profile(payload)
    if not policy["allowed"]:
        messages = "; ".join(v["message"] for v in policy["violations"])
        raise ValueError(messages)
    now = utcnow()
    with db()() as session:
        version = int(
            session.scalar(
                select(func.max(GtmMarketProfile.version)).where(
                    GtmMarketProfile.workspace_id == workspace_id
                )
            )
            or 0
        ) + 1
        row = GtmMarketProfile(
            id=new_id("gtmp"),
            workspace_id=workspace_id,
            version=version,
            name=(name or f"Market profile v{version}").strip()[:160],
            status="draft",
            seller_json=_dump(seller or {}),
            product_json=_dump(product or {}),
            segment_json=_dump(segment or {}),
            constraints_json=_dump(constraints or {}),
            evidence_json=_dump(evidence or []),
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        session.add(row)
        session.commit()
        return {**_profile_dict(row), "policy": policy}


def list_market_profiles(workspace_id: str) -> list[dict[str, Any]]:
    with db()() as session:
        rows = list(
            session.scalars(
                select(GtmMarketProfile)
                .where(GtmMarketProfile.workspace_id == workspace_id)
                .order_by(GtmMarketProfile.version.desc())
            )
        )
        return [_profile_dict(row) for row in rows]


def get_market_profile(workspace_id: str, profile_id: str) -> dict[str, Any] | None:
    with db()() as session:
        row = session.get(GtmMarketProfile, profile_id)
        if row is None or row.workspace_id != workspace_id:
            return None
        return _profile_dict(row)


def _weighted_archetypes(profile: dict[str, Any]) -> dict[str, float]:
    product = profile.get("product") or {}
    segment = profile.get("segment") or {}
    text = " ".join(
        str(v)
        for v in (
            segment.get("industry"),
            segment.get("company_stage"),
            segment.get("gtm_maturity"),
            segment.get("urgency"),
            segment.get("regulation"),
            product.get("business_model"),
            product.get("complexity"),
            product.get("switching_cost"),
            product.get("time_to_value"),
        )
        if v is not None
    ).lower()
    scores = {"accelerator": 1.0, "transformer": 1.0, "convenience": 1.0}
    if any(x in text for x in ("seed", "series a", "series b", "startup", "urgent", "founder-led")):
        scores["accelerator"] += 2.5
    if any(x in text for x in ("enterprise", "legal", "finance", "regulated", "high", "legacy", "services")):
        scores["transformer"] += 2.5
    if any(x in text for x in ("consumer", "b2c", "marketplace", "daily", "minutes", "low")):
        scores["convenience"] += 2.5
    if str(product.get("time_to_value") or "").lower() in {"hours", "days", "immediate", "short"}:
        scores["accelerator"] += 1.0
        scores["convenience"] += 0.5
    if str(product.get("switching_cost") or "").lower() in {"high", "very_high"}:
        scores["transformer"] += 1.5
    total = sum(scores.values())
    return {key: round(value / total, 3) for key, value in scores.items()}


def _ranked_evidence(profile: dict[str, Any]) -> list[dict[str, Any]]:
    evidence = [dict(item) for item in profile.get("evidence") or [] if isinstance(item, dict)]
    for item in evidence:
        klass = str(item.get("evidence_class") or "llm_inference")
        item["evidence_class"] = klass if klass in EVIDENCE_CLASSES else "llm_inference"
        item["rank"] = EVIDENCE_CLASSES[item["evidence_class"]]
        try:
            item["confidence"] = max(0.0, min(float(item.get("confidence", 0.5)), 1.0))
        except (TypeError, ValueError):
            item["confidence"] = 0.5
    return sorted(evidence, key=lambda item: (item["rank"], item["confidence"]), reverse=True)


def _baseline_thesis(profile: dict[str, Any]) -> dict[str, Any]:
    seller = profile.get("seller") or {}
    product = profile.get("product") or {}
    segment = profile.get("segment") or {}
    constraints = profile.get("constraints") or {}
    archetype = _weighted_archetypes(profile)
    dominant = max(archetype, key=archetype.get)  # type: ignore[arg-type]

    if dominant == "transformer":
        motivations = ["risk_reduction", "workflow_compatibility", "quantified_roi"]
        objections = ["implementation_risk", "security", "change_management"]
        proof = ["relevant_case_study", "security_evidence", "bounded_pilot"]
        offer = "A low-risk pilot with one team and an agreed success metric."
        channels = {"founder_email": 35, "warm_intro": 30, "industry_network": 20, "linkedin": 15}
        decision_speed = "medium_to_slow"
        relationship = "high"
    elif dominant == "convenience":
        motivations = ["convenience", "time_saved", "clear_economic_value"]
        objections = ["price_friction", "habit_change", "trust"]
        proof = ["visible_time_to_value", "social_proof", "transparent_pricing"]
        offer = "A low-friction trial or entry tier that demonstrates value immediately."
        channels = {"performance": 35, "referral": 25, "social": 20, "lifecycle": 20}
        decision_speed = "fast"
        relationship = "low_to_medium"
    else:
        motivations = ["speed", "output", "headcount_leverage"]
        objections = ["reliability", "priority", "integration_time"]
        proof = ["specific_workflow_demo", "quantified_output", "peer_case_study"]
        offer = "A focused workflow trial that produces a useful result this week."
        channels = {"founder_email": 35, "linkedin": 25, "warm_intro": 25, "content": 15}
        decision_speed = "fast"
        relationship = "medium"

    observed = segment.get("observed_behaviors") or []
    evidence = _ranked_evidence(profile)
    assumptions: list[str] = []
    required = {
        "product.time_to_value": product.get("time_to_value"),
        "product.switching_cost": product.get("switching_cost"),
        "segment.company_stage": segment.get("company_stage"),
        "segment.buyer_roles": segment.get("buyer_roles"),
        "segment.urgency": segment.get("urgency"),
    }
    assumptions.extend(f"{key} is unknown." for key, value in required.items() if not value)
    if not observed:
        assumptions.append("No first-party behavior supplied; channel and motivation choices are hypotheses.")
    if not evidence:
        assumptions.append("No cited evidence supplied; confidence is capped until research or outcomes exist.")

    evidence_strength = (
        sum(item["rank"] * item["confidence"] for item in evidence)
        / max(sum(item["rank"] for item in evidence), 1)
        if evidence
        else 0.25
    )
    completeness = (len(required) - len([v for v in required.values() if not v])) / len(required)
    confidence = round(min(0.85, 0.25 + 0.35 * completeness + 0.35 * evidence_strength), 2)
    countries = segment.get("countries") or ([segment["country"]] if segment.get("country") else [])
    buyer_roles = segment.get("buyer_roles") or ["economic_buyer", "champion", "technical_evaluator"]

    return {
        "archetype": {"weights": archetype, "dominant": dominant},
        "buying_profile": {
            "primary_motivations": motivations,
            "secondary_motivations": ["cost_effectiveness"],
            "objections": objections,
            "proof_required": proof,
            "decision_speed": decision_speed,
            "relationship_requirement": relationship,
            "pricing_sensitivity": "unknown_until_tested",
            "buyer_roles": buyer_roles,
            "observed_behaviors": observed,
        },
        "strategy": {
            "target": {
                "industries": segment.get("industries") or [segment.get("industry") or "unspecified"],
                "company_stage": segment.get("company_stage") or "unspecified",
                "countries": countries,
                "buyer_roles": buyer_roles,
            },
            "why_now": segment.get("trigger") or segment.get("urgency") or "Needs validation through signal research.",
            "channel_portfolio": channels,
            "message_angle": "Lead with the primary buying job, a specific observed signal, and relevant proof.",
            "offer": offer,
            "proof": proof,
            "cadence": {
                "mode": "relationship_first",
                "sequence": ["signal", "congratulate", "befriend", "offer", "convert"],
                "timezone_aware": True,
                "languages": constraints.get("languages") or ["en"],
            },
            "success_metric": "qualified_meeting_rate",
            "guardrails": {
                "max_complaint_rate": 0.001,
                "max_bounce_rate": 0.03,
                "stop_on_opt_out": True,
                "require_human_approval": True,
            },
            "experiment": {
                "hypothesis": (
                    f"For {segment.get('company_stage') or 'target'} "
                    f"{segment.get('industry') or 'accounts'}, a {motivations[0]} message "
                    "with relevant proof will increase qualified meeting rate."
                ),
                "primary_metric": "qualified_meeting_rate",
                "threshold": 0.05,
                "minimum_sample_size": 30,
                "window_days": 14,
                "variants": [
                    {"id": "motivation", "angle": motivations[0], "allocation": 50},
                    {"id": "proof", "angle": proof[0], "allocation": 50},
                ],
            },
        },
        "assumptions": assumptions,
        "evidence": evidence,
        "confidence": confidence,
        "seller_summary": seller.get("name") or "Supymem customer",
        "product_summary": product.get("name") or product.get("category") or "Product",
    }


def _extract_json(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", raw, re.DOTALL)
    if fenced:
        raw = fenced.group(1)
    else:
        start, end = raw.find("{"), raw.rfind("}")
        if start >= 0 and end > start:
            raw = raw[start : end + 1]
    parsed = json.loads(raw)
    return parsed if isinstance(parsed, dict) else {}


async def _llm_refine(
    profile: dict[str, Any], baseline: dict[str, Any], workspace_id: str
) -> tuple[dict[str, Any], str]:
    from app.credits.metered import llm
    from app.llm.founder_provider import resolve_llm_config

    prompt = {
        "task": "Refine this GTM thesis using only supplied evidence and context.",
        "rules": [
            "Return one JSON object only.",
            "Keep the same top-level keys: archetype, buying_profile, strategy, assumptions, evidence, confidence.",
            "Do not infer race, ethnicity, caste, religion, health, disability, sexuality, politics, age, or culture.",
            "Geography may affect language, law, timing, payment, channel availability and explicitly cited market evidence only.",
            "Unknown facts remain unknown. Add unsupported claims to assumptions.",
            "Observed outcomes outrank segment data, research, priors and LLM inference.",
            "Channel portfolio integer percentages must total 100.",
        ],
        "profile": profile,
        "deterministic_baseline": baseline,
    }
    result = await llm(
        [
            {
                "role": "system",
                "content": (
                    "You are Supymem's evidence-first GTM strategist. You propose "
                    "falsifiable, non-discriminatory strategy; you do not invent cultural facts."
                ),
            },
            {"role": "user", "content": _dump(prompt)},
        ],
        purpose="gtm.market_thesis",
        tier="pro",
        max_tokens=6144,
        response_schema={"type": "object"},
        workspace_id=workspace_id,
    )
    choices = getattr(result, "choices", None) or []
    content = str(getattr(getattr(choices[0], "message", None), "content", "") or "") if choices else ""
    if not content:
        raise ValueError("LLM returned no final JSON content")
    parsed = _extract_json(content)
    required = {"archetype", "buying_profile", "strategy", "assumptions", "evidence"}
    if not required.issubset(parsed):
        raise ValueError("LLM thesis did not satisfy the structured contract")
    return parsed, str(getattr(result, "model", "") or resolve_llm_config(workspace_id).model)


async def generate_thesis(
    workspace_id: str,
    profile_id: str,
    *,
    use_llm: bool = True,
) -> dict[str, Any]:
    profile = get_market_profile(workspace_id, profile_id)
    if profile is None:
        raise LookupError("market profile not found")
    profile_policy = assess_profile(profile)
    if not profile_policy["allowed"]:
        raise ValueError("market profile is blocked by policy")

    generated = _baseline_thesis(profile)
    model = "deterministic-v1"
    generation_warnings: list[str] = []
    if use_llm:
        try:
            refined, model = await _llm_refine(profile, generated, workspace_id)
            # Evidence remains anchored to persisted inputs. The model may reorder
            # or summarize but cannot manufacture a higher-ranked source.
            refined["evidence"] = generated["evidence"]
            refined["confidence"] = min(
                float(refined.get("confidence") or generated["confidence"]),
                0.9 if generated["evidence"] else 0.45,
            )
            generated = refined
        except Exception:  # noqa: BLE001
            generation_warnings.append("AI refinement is unavailable right now. The evidence-based draft is ready to review.")

    generated["confidence"] = max(0.0, min(float(generated.get("confidence") or 0), 1.0))
    thesis_policy = assess_thesis(generated, profile_policy)
    if generation_warnings:
        thesis_policy["warnings"] = list(thesis_policy["warnings"]) + generation_warnings
    now = utcnow()
    with db()() as session:
        version = int(
            session.scalar(
                select(func.max(GtmThesis.version)).where(
                    GtmThesis.profile_id == profile_id
                )
            )
            or 0
        ) + 1
        row = GtmThesis(
            id=new_id("gtmt"),
            workspace_id=workspace_id,
            profile_id=profile_id,
            version=version,
            status="draft",
            archetype_json=_dump(generated.get("archetype") or {}),
            buying_profile_json=_dump(generated.get("buying_profile") or {}),
            strategy_json=_dump(generated.get("strategy") or {}),
            assumptions_json=_dump(generated.get("assumptions") or []),
            evidence_json=_dump(generated.get("evidence") or []),
            policy_json=_dump(thesis_policy),
            confidence=generated["confidence"],
            model=model,
            created_at=now,
        )
        session.add(row)
        session.commit()
        return _thesis_dict(row)


def list_theses(workspace_id: str, profile_id: str | None = None) -> list[dict[str, Any]]:
    with db()() as session:
        stmt = select(GtmThesis).where(GtmThesis.workspace_id == workspace_id)
        if profile_id:
            stmt = stmt.where(GtmThesis.profile_id == profile_id)
        rows = list(session.scalars(stmt.order_by(GtmThesis.created_at.desc())))
        return [_thesis_dict(row) for row in rows]


def get_thesis(workspace_id: str, thesis_id: str) -> dict[str, Any] | None:
    with db()() as session:
        row = session.get(GtmThesis, thesis_id)
        if row is None or row.workspace_id != workspace_id:
            return None
        return _thesis_dict(row)


def get_active_thesis(workspace_id: str) -> dict[str, Any] | None:
    with db()() as session:
        row = session.scalar(
            select(GtmThesis)
            .where(
                GtmThesis.workspace_id == workspace_id,
                GtmThesis.status == "approved",
            )
            .order_by(GtmThesis.approved_at.desc())
        )
        return _thesis_dict(row) if row else None


def approve_thesis(
    workspace_id: str, thesis_id: str, *, actor_id: str
) -> dict[str, Any]:
    with db()() as session:
        row = session.get(GtmThesis, thesis_id)
        if row is None or row.workspace_id != workspace_id:
            raise LookupError("thesis not found")
        policy = _load(row.policy_json, {})
        if not policy.get("allowed"):
            raise ValueError("thesis is blocked by policy")
        for old in session.scalars(
            select(GtmThesis).where(
                GtmThesis.workspace_id == workspace_id,
                GtmThesis.status == "approved",
                GtmThesis.id != row.id,
            )
        ):
            old.status = "superseded"
            for experiment in session.scalars(
                select(GtmCampaignExperiment).where(
                    GtmCampaignExperiment.thesis_id == old.id,
                    GtmCampaignExperiment.status == "running",
                )
            ):
                experiment.status = "paused"
        for profile in session.scalars(
            select(GtmMarketProfile).where(
                GtmMarketProfile.workspace_id == workspace_id,
                GtmMarketProfile.status == "active",
                GtmMarketProfile.id != row.profile_id,
            )
        ):
            profile.status = "archived"
            profile.updated_at = utcnow()
        target_profile = session.get(GtmMarketProfile, row.profile_id)
        if target_profile:
            target_profile.status = "active"
            target_profile.updated_at = utcnow()
        row.status = "approved"
        row.approved_by = actor_id
        row.approved_at = utcnow()
        session.commit()
        return _thesis_dict(row)
