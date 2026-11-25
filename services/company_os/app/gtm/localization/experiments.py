"""Compile approved theses into experiments and aggregate commercial outcomes."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from typing import Any

from sqlalchemy import select

from app.db.models import GtmCampaignExperiment, GtmOutcome, GtmThesis
from app.gtm.localization.service import get_active_thesis, get_thesis
from app.state import db
from core.clock import utcnow
from core.ids import new_id


def _load(raw: str | None, default: Any) -> Any:
    try:
        return json.loads(raw or "")
    except (json.JSONDecodeError, TypeError):
        return default


def _serialize(row: GtmCampaignExperiment) -> dict[str, Any]:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "thesis_id": row.thesis_id,
        "hypothesis": row.hypothesis,
        "primary_metric": row.primary_metric,
        "threshold": row.threshold,
        "minimum_sample_size": row.minimum_sample_size,
        "window_days": row.window_days,
        "variants": _load(row.variants_json, []),
        "allocation": _load(row.allocation_json, {}),
        "guardrails": _load(row.guardrails_json, {}),
        "status": row.status,
        "started_at": row.started_at.isoformat() if row.started_at else None,
        "stopped_at": row.stopped_at.isoformat() if row.stopped_at else None,
        "created_at": row.created_at.isoformat(),
    }


def execution_context(workspace_id: str) -> dict[str, Any]:
    """Return the strategy workers may use.

    Legacy workspaces continue to run until their first Market Profile exists.
    Once profiles exist, an approved thesis is mandatory.
    """
    from app.db.models import GtmMarketProfile

    with db()() as session:
        has_profile = (
            session.scalar(
                select(GtmMarketProfile.id)
                .where(GtmMarketProfile.workspace_id == workspace_id)
                .limit(1)
            )
            is not None
        )
    thesis = get_active_thesis(workspace_id)
    if thesis:
        return {"allowed": True, "mode": "thesis", "thesis": thesis}
    if has_profile:
        return {
            "allowed": False,
            "mode": "approval_required",
            "reason": "Approve a GTM Thesis Card before discovery or outreach.",
            "thesis": None,
        }
    return {"allowed": True, "mode": "legacy", "thesis": None}


def compile_experiment(workspace_id: str, thesis_id: str) -> dict[str, Any]:
    thesis = get_thesis(workspace_id, thesis_id)
    if thesis is None:
        raise LookupError("thesis not found")
    if thesis["status"] != "approved":
        raise ValueError("approve the thesis before compiling an experiment")
    strategy = thesis.get("strategy") or {}
    spec = strategy.get("experiment") or {}
    variants = spec.get("variants") or [
        {"id": "motivation", "angle": "primary_motivation", "allocation": 50},
        {"id": "proof", "angle": "primary_proof", "allocation": 50},
    ]
    allocation = {
        str(v.get("id") or f"variant_{idx + 1}"): int(v.get("allocation") or 0)
        for idx, v in enumerate(variants)
        if isinstance(v, dict)
    }
    if sum(allocation.values()) != 100:
        equal = 100 // max(len(allocation), 1)
        allocation = {key: equal for key in allocation}
        if allocation:
            allocation[next(iter(allocation))] += 100 - sum(allocation.values())
    row = GtmCampaignExperiment(
        id=new_id("gtmexp"),
        workspace_id=workspace_id,
        thesis_id=thesis_id,
        hypothesis=str(spec.get("hypothesis") or "Approved GTM thesis improves qualified outcomes."),
        primary_metric=str(spec.get("primary_metric") or "qualified_meeting_rate"),
        threshold=float(spec.get("threshold") or 0.05),
        minimum_sample_size=max(10, int(spec.get("minimum_sample_size") or 30)),
        window_days=max(1, int(spec.get("window_days") or 14)),
        variants_json=json.dumps(variants),
        allocation_json=json.dumps(allocation),
        guardrails_json=json.dumps(strategy.get("guardrails") or {}),
        status="planned",
        created_at=utcnow(),
    )
    with db()() as session:
        session.add(row)
        session.commit()
        return _serialize(row)


def list_experiments(workspace_id: str) -> list[dict[str, Any]]:
    with db()() as session:
        rows = list(
            session.scalars(
                select(GtmCampaignExperiment)
                .where(GtmCampaignExperiment.workspace_id == workspace_id)
                .order_by(GtmCampaignExperiment.created_at.desc())
            )
        )
        return [_serialize(row) for row in rows]


def set_experiment_status(
    workspace_id: str, experiment_id: str, status: str
) -> dict[str, Any]:
    allowed = {"running", "paused", "completed", "cancelled"}
    if status not in allowed:
        raise ValueError(f"status must be one of {sorted(allowed)}")
    with db()() as session:
        row = session.get(GtmCampaignExperiment, experiment_id)
        if row is None or row.workspace_id != workspace_id:
            raise LookupError("experiment not found")
        thesis = session.get(GtmThesis, row.thesis_id)
        if status == "running" and (thesis is None or thesis.status != "approved"):
            raise ValueError("an approved thesis is required to run an experiment")
        row.status = status
        if status == "running" and row.started_at is None:
            row.started_at = utcnow()
        if status in {"completed", "cancelled"}:
            row.stopped_at = utcnow()
        session.commit()
        return _serialize(row)


def record_outcome(
    workspace_id: str,
    kind: str,
    *,
    thesis_id: str | None = None,
    experiment_id: str | None = None,
    contact_id: str | None = None,
    channel: str | None = None,
    variant: str | None = None,
    value: float | None = None,
    currency: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    if not thesis_id:
        active = get_active_thesis(workspace_id)
        thesis_id = active["id"] if active else None
    if not experiment_id and thesis_id:
        with db()() as session:
            running = session.scalar(
                select(GtmCampaignExperiment)
                .where(
                    GtmCampaignExperiment.workspace_id == workspace_id,
                    GtmCampaignExperiment.thesis_id == thesis_id,
                    GtmCampaignExperiment.status == "running",
                )
                .order_by(GtmCampaignExperiment.started_at.desc())
            )
            experiment_id = running.id if running else None
    now = utcnow()
    row = GtmOutcome(
        id=new_id("gtmo"),
        workspace_id=workspace_id,
        thesis_id=thesis_id,
        experiment_id=experiment_id,
        contact_id=contact_id,
        kind=kind,
        channel=channel,
        variant=variant,
        value=value,
        currency=currency,
        metadata_json=json.dumps(metadata or {}),
        occurred_at=now,
        created_at=now,
    )
    outcome_id = row.id
    with db()() as session:
        session.add(row)
        session.commit()
    return {"id": outcome_id, "thesis_id": thesis_id, "experiment_id": experiment_id}


def summarize_experiment(workspace_id: str, experiment_id: str) -> dict[str, Any]:
    with db()() as session:
        exp = session.get(GtmCampaignExperiment, experiment_id)
        if exp is None or exp.workspace_id != workspace_id:
            raise LookupError("experiment not found")
        rows = list(
            session.scalars(
                select(GtmOutcome).where(
                    GtmOutcome.workspace_id == workspace_id,
                    GtmOutcome.experiment_id == experiment_id,
                )
            )
        )
    by_kind = Counter(row.kind for row in rows)
    by_variant: dict[str, Counter[str]] = defaultdict(Counter)
    for row in rows:
        by_variant[row.variant or "unassigned"][row.kind] += 1
    sent = by_kind["sent"] + by_kind["linkedin_sent"]
    qualified = by_kind["qualified_meeting"] + by_kind["customer"]
    revenue = sum(
        float(row.value or 0)
        for row in rows
        if row.kind in {"revenue", "customer"} and row.value is not None
    )
    observed = qualified / sent if sent else 0.0
    sample_ready = sent >= exp.minimum_sample_size
    guardrail_breached = (
        by_kind["complaint"] > 0
        or (by_kind["bounce"] / sent if sent else 0) > 0.03
    )
    if guardrail_breached:
        decision = "pause"
    elif not sample_ready:
        decision = "collect_more_evidence"
    elif observed >= exp.threshold:
        decision = "continue"
    else:
        decision = "propose_new_thesis"
    return {
        "experiment": _serialize(exp),
        "sample_size": sent,
        "minimum_sample_size": exp.minimum_sample_size,
        "sample_ready": sample_ready,
        "outcomes": dict(by_kind),
        "variants": {key: dict(value) for key, value in by_variant.items()},
        "observed_primary_metric": round(observed, 4),
        "revenue": revenue,
        "decision": decision,
        "guardrail_breached": guardrail_breached,
    }
