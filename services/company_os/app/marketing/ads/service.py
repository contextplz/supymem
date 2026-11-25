"""Durable, approval-backed paid-campaign workflow.

This module deliberately owns a narrow write surface: create a complete campaign
proposal, obtain an approval, then create one provider campaign in PAUSED state.
It never activates delivery or exposes arbitrary provider API calls.
"""

from __future__ import annotations

import json
import math
import re
from typing import Any
from urllib.parse import urlparse

from core.clock import utcnow
from core.ids import new_id
from sqlalchemy import select

from app.approvals import service as approvals
from app.audit.writer import audit
from app.db.models import MarketingAdCampaign, MarketingAdProposal
from app.marketing.media import probes
from app.state import db

PLATFORMS = frozenset({"google", "meta"})
GOALS = frozenset({"leads", "sales", "traffic"})
MAX_DAILY_BUDGET_USD = 25.0


class AdsError(ValueError):
    def __init__(self, code: str, message: str, status: int = 422) -> None:
        super().__init__(message)
        self.code = code
        self.status = status


def _json(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _proposal_view(row: MarketingAdProposal) -> dict[str, Any]:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "platform": row.platform,
        "status": row.status,
        "idea": row.idea,
        "blueprint": _json(row.blueprint_json),
        "approval_id": row.approval_id,
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _campaign_view(row: MarketingAdCampaign) -> dict[str, Any]:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "proposal_id": row.proposal_id,
        "platform": row.platform,
        "external_id": row.external_id,
        "name": row.name,
        "objective": row.objective,
        "status": row.status,
        "daily_budget_usd": row.daily_budget_usd,
        "provider_response": _json(row.provider_response_json),
        "created_at": row.created_at.isoformat(),
        "updated_at": row.updated_at.isoformat(),
    }


def _title(idea: str) -> str:
    words = re.findall(r"[A-Za-z0-9]+", idea)
    return " ".join(words[:5]) or "New campaign"


def _short(value: str, limit: int) -> str:
    """Trim provider copy at a word boundary while preserving a useful fallback."""
    normalized = " ".join(value.split())
    if len(normalized) <= limit:
        return normalized
    cut = normalized[: limit + 1].rsplit(" ", 1)[0].strip()
    return cut or normalized[:limit].strip()


def _keywords(idea: str, audience: str) -> list[str]:
    """Make a conservative starter keyword set; founders can refine it in review."""
    phrases = [_title(idea)]
    phrases.extend(part.strip() for part in audience.split(","))
    words = re.findall(r"[A-Za-z0-9]+", idea.lower())
    if len(words) >= 2:
        phrases.append(" ".join(words[:3]))
    unique: list[str] = []
    for phrase in phrases:
        cleaned = _short(" ".join(re.findall(r"[A-Za-z0-9]+", phrase)), 80)
        if len(cleaned) >= 3 and cleaned.lower() not in {x.lower() for x in unique}:
            unique.append(cleaned)
    return unique[:8] or ["new customers"]


def _valid_landing_url(value: str) -> str:
    # Check before strip/urlparse: the parser removes some literal controls.
    if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
        raise AdsError("landing_url_invalid", "Use a complete http(s) landing-page URL without credentials or control characters.")
    url = value.strip()
    try:
        parsed = urlparse(url)
        valid = (parsed.scheme in {"http", "https"} and bool(parsed.netloc)
                 and parsed.username is None and parsed.password is None)
    except ValueError as exc:
        raise AdsError("landing_url_invalid", "Use a complete http(s) landing-page URL.") from exc
    if not valid:
        raise AdsError(
            "landing_url_invalid", "Use a complete http(s) landing-page URL without credentials."
        )
    return url


def _goal(value: str | None) -> str:
    goal = (value or "leads").strip().lower()
    if goal not in GOALS:
        raise AdsError("goal_invalid", "Goal must be leads, sales, or traffic.")
    return goal


def _platform(value: str) -> str:
    platform = value.strip().lower()
    if platform not in PLATFORMS:
        raise AdsError("platform_invalid", "Choose Google or Meta.")
    return platform


def _budget(value: Any) -> float:
    try:
        budget = float(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise AdsError("budget_invalid", "Daily budget must be a finite number.") from exc
    if not math.isfinite(budget):
        raise AdsError("budget_invalid", "Daily budget must be a finite number.")
    budget = round(budget, 2)
    if budget <= 0 or budget > MAX_DAILY_BUDGET_USD:
        raise AdsError(
            "budget_outside_guardrail",
            f"Daily budget must be more than $0 and no more than ${MAX_DAILY_BUDGET_USD:.2f}.",
        )
    return budget


def _blueprint(payload: dict[str, Any]) -> dict[str, Any]:
    platform = _platform(str(payload.get("platform") or ""))
    idea = str(payload.get("idea") or "").strip()
    if len(idea) < 12:
        raise AdsError(
            "idea_too_short", "Describe the product or offer in at least 12 characters."
        )
    goal = _goal(payload.get("goal"))
    landing_url = _valid_landing_url(str(payload.get("landing_url") or ""))
    daily_budget = _budget(payload.get("daily_budget_usd"))
    audience = str(payload.get("audience") or "").strip()
    if len(audience) < 3:
        raise AdsError("audience_required", "Name the audience you want to reach.")
    geography = str(payload.get("geography") or "").strip() or "US"
    name = str(payload.get("name") or "").strip() or f"{_title(idea)} · {goal.title()}"
    if len(name) > 120:
        raise AdsError("name_too_long", "Campaign name must be at most 120 characters.")
    headline = str(payload.get("headline") or _title(idea)).strip()
    description = str(payload.get("description") or idea).strip()
    google_headline = _short(headline, 30)
    google_description = _short(description, 90)
    audience_headline = _short(f"For {audience.split(',')[0].strip()}", 30)
    objective = {
        "google": {"leads": "SEARCH", "sales": "SEARCH", "traffic": "SEARCH"},
        "meta": {
            "leads": "OUTCOME_LEADS",
            "sales": "OUTCOME_SALES",
            "traffic": "OUTCOME_TRAFFIC",
        },
    }[platform][goal]
    return {
        "name": name,
        "platform": platform,
        "goal": goal,
        "objective": objective,
        "daily_budget_usd": daily_budget,
        "landing_url": landing_url,
        "audience": audience,
        "geography": geography,
        "creative": {
            "headline": headline[:90],
            "description": description[:500],
            "google_headlines": [
                google_headline,
                _short("Get started today", 30),
                audience_headline,
            ],
            "google_descriptions": [
                google_description,
                _short(f"Learn how {headline} can help. Visit today.", 90),
            ],
            "meta_primary_text": description[:500],
        },
        "keywords": _keywords(idea, audience),
        "status_on_create": "PAUSED",
        "launch_requirements": [
            "Founder approval to spend",
            "Provider connection and credentials",
            "A verified landing page and conversion event",
        ],
    }


def create_proposal(workspace_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    blueprint = _blueprint(payload)
    now = utcnow()
    row = MarketingAdProposal(
        id=new_id("adp"),
        workspace_id=workspace_id,
        platform=blueprint["platform"],
        status="draft",
        idea=str(payload["idea"]).strip(),
        blueprint_json=json.dumps(blueprint, ensure_ascii=False),
        idempotency_key=new_id("adk"),
        created_at=now,
        updated_at=now,
    )
    with db()() as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        result = _proposal_view(row)
    audit(
        "ads.proposal.created",
        workspace_id=workspace_id,
        actor_id="agent:ads",
        actor_kind="agent",
        target_ref=row.id,
        surface="api",
        payload={
            "platform": row.platform,
            "goal": blueprint["goal"],
            "daily_budget_usd": blueprint["daily_budget_usd"],
        },
    )
    return result


def get_proposal(workspace_id: str, proposal_id: str) -> dict[str, Any]:
    with db()() as session:
        row = session.get(MarketingAdProposal, proposal_id)
        if row is None or row.workspace_id != workspace_id:
            raise AdsError("proposal_not_found", "Campaign proposal not found.", 404)
        return _proposal_view(row)


def list_proposals(workspace_id: str) -> list[dict[str, Any]]:
    with db()() as session:
        rows = session.scalars(
            select(MarketingAdProposal)
            .where(MarketingAdProposal.workspace_id == workspace_id)
            .order_by(MarketingAdProposal.created_at.desc())
        ).all()
        return [_proposal_view(row) for row in rows]


def request_launch(workspace_id: str, proposal_id: str) -> dict[str, Any]:
    with db()() as session:
        row = session.get(MarketingAdProposal, proposal_id)
        if row is None or row.workspace_id != workspace_id:
            raise AdsError("proposal_not_found", "Campaign proposal not found.", 404)
        if row.status not in {"draft", "rejected"}:
            raise AdsError(
                "proposal_not_draft",
                "This campaign is already awaiting a decision or was created.",
                409,
            )
        blueprint = _json(row.blueprint_json)
    approval = approvals.request_approval(
        workspace_id=workspace_id,
        kind="spend_increase",
        subject_ref=proposal_id,
        summary=f"Launch paused {blueprint.get('platform', 'ads')} campaign: {blueprint.get('name', proposal_id)}",
        requested_by="agent:ads",
        risk="high",
        detail={
            "action": "create_paused_campaign_then_allow_founder_launch",
            "platform": blueprint.get("platform"),
            "objective": blueprint.get("objective"),
            "daily_budget_usd": blueprint.get("daily_budget_usd"),
            "landing_url": blueprint.get("landing_url"),
            "audience": blueprint.get("audience"),
            "geography": blueprint.get("geography"),
            "status_on_create": "PAUSED",
        },
    )
    now = utcnow()
    with db()() as session:
        row = session.get(MarketingAdProposal, proposal_id)
        if row is None:
            raise AdsError("proposal_not_found", "Campaign proposal not found.", 404)
        row.approval_id = approval.id
        row.status = "pending_approval"
        row.updated_at = now
        session.commit()
        result = _proposal_view(row)
    result["approval"] = approval.to_dict()
    return result


def decide_launch(
    workspace_id: str, proposal_id: str, *, decision: str, decided_by: str
) -> dict[str, Any]:
    proposal = get_proposal(workspace_id, proposal_id)
    approval_id = str(proposal.get("approval_id") or "")
    if not approval_id:
        raise AdsError("approval_missing", "Request launch approval first.", 409)
    try:
        approval = approvals.decide(
            approval_id, decision=decision, decided_by=decided_by, surface="api"
        )
    except approvals.ApprovalError as exc:
        raise AdsError(
            exc.code,
            exc.message,
            409 if exc.code in {"already_decided", "expired"} else 403,
        ) from exc
    now = utcnow()
    with db()() as session:
        row = session.get(MarketingAdProposal, proposal_id)
        if row is None or row.workspace_id != workspace_id:
            raise AdsError("proposal_not_found", "Campaign proposal not found.", 404)
        row.status = "approved" if decision == "approved" else "rejected"
        row.updated_at = now
        session.commit()
        result = _proposal_view(row)
    result["approval"] = approval.to_dict()
    return result


def create_paused(workspace_id: str, proposal_id: str) -> dict[str, Any]:
    with db()() as session:
        proposal = session.get(MarketingAdProposal, proposal_id)
        if proposal is None or proposal.workspace_id != workspace_id:
            raise AdsError("proposal_not_found", "Campaign proposal not found.", 404)
        existing = session.scalar(
            select(MarketingAdCampaign).where(
                MarketingAdCampaign.proposal_id == proposal_id
            )
        )
        if existing is not None:
            return {"ok": True, "campaign": _campaign_view(existing), "replayed": True}
        if proposal.status != "approved":
            raise AdsError(
                "approval_required", "Approve this campaign before creating it.", 409
            )
        blueprint = _json(proposal.blueprint_json)
        approval_id = proposal.approval_id

    outcome = probes.create_paused_ad(
        platform=str(blueprint["platform"]),
        name=str(blueprint["name"]),
        daily_budget=str(blueprint["daily_budget_usd"]),
        campaign_spec=blueprint,
    )
    if (
        not outcome.get("ok")
        or str((outcome.get("result") or {}).get("status") or "").upper() != "PAUSED"
    ):
        now = utcnow()
        with db()() as session:
            proposal = session.get(MarketingAdProposal, proposal_id)
            if proposal is not None:
                proposal.status = "blocked"
                proposal.updated_at = now
                session.commit()
        audit(
            "ads.campaign.blocked",
            workspace_id=workspace_id,
            actor_id="agent:ads",
            actor_kind="agent",
            target_ref=proposal_id,
            approval_id=approval_id,
            outcome="blocked",
            denied_reason=str(
                outcome.get("error") or outcome.get("block_reason") or "provider_failed"
            ),
            payload={"platform": blueprint.get("platform")},
        )
        return {
            "ok": False,
            "proposal": get_proposal(workspace_id, proposal_id),
            "outcome": outcome,
        }

    result = outcome.get("result") or {}
    external_id = str(result.get("resource_name") or result.get("campaign_id") or "")
    if not external_id:
        raise AdsError(
            "provider_reference_missing",
            "Provider did not return a campaign reference.",
            502,
        )
    now = utcnow()
    with db()() as session:
        proposal = session.get(MarketingAdProposal, proposal_id)
        if proposal is None or proposal.workspace_id != workspace_id:
            raise AdsError("proposal_not_found", "Campaign proposal not found.", 404)
        existing = session.scalar(
            select(MarketingAdCampaign).where(
                MarketingAdCampaign.proposal_id == proposal_id
            )
        )
        if existing is not None:
            return {"ok": True, "campaign": _campaign_view(existing), "replayed": True}
        campaign = MarketingAdCampaign(
            id=new_id("adc"),
            workspace_id=workspace_id,
            proposal_id=proposal_id,
            platform=proposal.platform,
            external_id=external_id,
            name=str(blueprint["name"]),
            objective=str(blueprint["objective"]),
            status="PAUSED",
            daily_budget_usd=float(blueprint["daily_budget_usd"]),
            provider_response_json=json.dumps(outcome, default=str),
            created_at=now,
            updated_at=now,
        )
        proposal.status = "created"
        proposal.updated_at = now
        session.add(campaign)
        session.commit()
        session.refresh(campaign)
        view = _campaign_view(campaign)
        approval_id = proposal.approval_id
    audit(
        "ads.campaign.created_paused",
        workspace_id=workspace_id,
        actor_id="agent:ads",
        actor_kind="agent",
        target_ref=view["id"],
        approval_id=approval_id,
        payload={
            "platform": view["platform"],
            "external_id": external_id,
            "daily_budget_usd": view["daily_budget_usd"],
        },
    )
    return {"ok": True, "campaign": view, "proof": outcome.get("proof")}


def list_campaigns(workspace_id: str) -> list[dict[str, Any]]:
    with db()() as session:
        rows = session.scalars(
            select(MarketingAdCampaign)
            .where(MarketingAdCampaign.workspace_id == workspace_id)
            .order_by(MarketingAdCampaign.created_at.desc())
        ).all()
        return [_campaign_view(row) for row in rows]
