"""Synthetic-only local evaluation through the real durable, metered lifecycle."""
from __future__ import annotations

import asyncio
import base64
import json
import os

from core.clock import utcnow
from core.ids import new_id
from sqlalchemy import select

from app.core.local_mode import is_local
from app.pagebuilder import api
from app.state import db

_WORKSPACE = None
SLUGS = {"nightshift", "smallhours", "patchwork", "nearbyhands", "afterimage", "margin", "wondertrail", "releaf"}


def run_evaluation(fixture, *, recent_directions=(), cap_usd=1, mode="live"):
    if mode != "live" or fixture.get("slug") not in SLUGS:
        raise ValueError("pagebuilder_eval_requires_known_synthetic_fixture")
    # Never inherit a database URL from a developer's production .env.
    url = os.environ.get("COMPANY_OS_DB_URL", "")
    if not url.startswith("sqlite:///") or not os.environ.get("COMPANY_OS_DATA_DIR"):
        raise ValueError("pagebuilder_eval_requires_explicit_isolated_sqlite")
    from app.authz.workspace import ensure_subject_person
    from app.credits.accounts import ensure_account
    from app.db.models import Job, Venture, Workspace
    from app.db.supymem_models import IdeaRevision, IdeaSession
    from app.workspaces.service import create_workspace
    from app.core.jobs import lease
    from app.pagebuilder.worker import run_one

    global _WORKSPACE
    actor = "pagebuilder_synthetic_evaluation"
    if _WORKSPACE is None:
        ensure_subject_person(actor)
        with db()() as session:
            _WORKSPACE = session.scalar(select(Workspace.id).where(Workspace.owner_id == actor,
                Workspace.name == "Synthetic pagebuilder evaluation", Workspace.archived_at.is_(None)).limit(1))
        if _WORKSPACE is None:
            _WORKSPACE = create_workspace(kind="company", name="Synthetic pagebuilder evaluation", owner_id=actor).id
    wid = _WORKSPACE
    vid, sid, rid, now = new_id("ven"), new_id("ids"), new_id("idv"), utcnow()
    with db()() as session:
        session.add(Venture(id=vid, workspace_id=wid, name=fixture["name"], raw_idea=fixture["idea"],
            stage="draft", autonomy_level="L1", created_at=now, updated_at=now))
        session.flush()
        session.add(IdeaSession(id=sid, workspace_id=wid, venture_id=vid, case="know", state="confirmed",
            current_revision_id=rid, confirmed_revision_id=rid, confirmed_by_person_id=actor,
            confirmed_at=now, created_at=now, updated_at=now))
        session.flush()
        session.add(IdeaRevision(id=rid, session_id=sid, workspace_id=wid, venture_id=vid, rev=1, source="founder",
            summary=fixture["idea"], structured=api.dump({"audience": fixture["audience"], "category": fixture["category"]}), created_at=now))
        session.commit()
    ensure_account(vid, workspace_id=wid, owner_person_id=actor)
    quote = api.estimate(vid, actor)
    if is_local() and quote["ready"]:
        quote["ceiling_micro"] = api.resources(wid, vid)["ceiling_micro"]
    if not quote["ready"] or quote["ceiling_micro"] / 1000000 > cap_usd:
        raise ValueError("pagebuilder_eval_budget_or_provider_unavailable")
    accepted = api.start(vid, actor)
    jid = accepted["job_id"]
    factory = db()
    with factory() as session:
        claimed = lease(session, "pagebuilder-evaluation", kinds=[api.KIND], n=1)
        session.commit()
    target = next((job for job in claimed if job.id == jid), None)
    if target is None:
        raise ValueError("pagebuilder_eval_job_not_leased")
    asyncio.run(run_one(factory, target))
    with factory() as session:
        row = session.get(Job, jid)
        doc = json.loads(row.payload_json)
    operations = doc["_runtime"]["operations"]
    spend = sum(op.get("usage", {}).get("provider_actual_micro", op.get("usage", {}).get("actual_micro", 0)) for op in operations.values())
    unknown = [op for op in operations.values() if op.get("state") in {"calling", "uncertain"}
        and "actual_micro" not in op.get("usage", {})]
    from app.pagebuilder.models import PageInput
    grounding_input = PageInput.model_validate(doc["_runtime"]["input"]).model_dump(mode="json")
    receipt = {"metered": True, "request_policy": doc["_runtime"]["quote"].get("request_policy"),
        "provider": doc["_runtime"]["quote"].get("provider"), "model": doc["_runtime"]["quote"].get("model"),
        "reasoning_policy": doc["_runtime"]["quote"].get("reasoning_policy"),
        "call_timeout_seconds": doc["_runtime"]["quote"].get("call_timeout_seconds"),
        "grounding_input": grounding_input, "grounding_input_sha256": api.digest(grounding_input),
        "provider_cost_usd": spend / 1000000,
        "credits": sum(op.get("usage", {}).get("credits_micro", 0) for op in operations.values()) / 10000,
        "ceiling_usd": quote["ceiling_micro"] / 1000000, "calls": doc["_runtime"]["calls_used"], "job_id": jid,
        "uncertain_outcomes": len(unknown), "unresolved_ceiling_usd": sum(op.get("usage", {}).get("estimate_micro", 0) for op in unknown) / 1000000}
    from app.pagebuilder.pipeline import _json
    from app.pagebuilder.models import CapabilityReview
    receipt["grounding_operations"] = []
    for key, op in operations.items():
        if key.split(":", 1)[-1] not in {"capability_review_1", "capability_review_2", "capability_review_3", "capability_review_4", "capability_review_5", "capability_review_6"}:
            continue
        try:
            review_digest = api.digest(CapabilityReview.model_validate(_json(op["response"]["content"])).model_dump(mode="json"))
        except (KeyError, TypeError, ValueError):
            review_digest = None
        receipt["grounding_operations"].append({"stage": key.split(":", 1)[1], "operation_key": key,
            "provider": op.get("usage", {}).get("provider"), "model": op.get("usage", {}).get("model"),
            "state": op.get("state"), "settled": op.get("usage", {}).get("settled") is True,
            "review_sha256": review_digest})
    from pathlib import Path
    receipts = Path(os.environ["COMPANY_OS_DATA_DIR"]) / "pagebuilder-eval-receipts"
    receipts.mkdir(exist_ok=True)
    (receipts / (fixture["slug"] + "-" + jid + ".json")).write_text(api.dump(receipt))
    if api.status(vid, actor, jid)["state"] != "ready":
        raise ValueError(doc["_runtime"].get("error", "pagebuilder_eval_failed"))
    from app.conversion.waitlist import public_details
    details = public_details(vid)
    bundle = doc["result"]["bundles"][0]
    return {"files": {path: base64.b64decode(body) for path, body in bundle["files"].items()},
        "screenshots": {path: base64.b64decode(body) for path, body in bundle["screenshots"].items()},
        "direction": bundle["direction"], "plan": bundle["plan"], "manifest": bundle["manifest"], "qa": bundle["qa"],
        "spec_markdown": bundle["spec_markdown"], "cost_receipt": {**receipt, "waitlist_url": details["url"],
            "waitlist_backend": {"configured": True, "local_details_verified": True,
                "consent_version": details["consent_version"], "consent_text": details["consent"],
                "external_submission_verified": False}}}
