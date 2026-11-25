"""Plan compile receipts in ExecutionPlan.structured, one row per durable job.

The plan owns its immutable input snapshot and response journal. Existing normalized
milestone/task rows are created only after a validated, settled response, under the
same lease fence. No new run table or migration is needed.
"""
from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from sqlalchemy import func, select

from app.core.jobs import FatalError
from app.core.local_mode import is_local
from app.core.operation_receipts import OperationJournal
from app.db.models import Job, Venture
from app.db.supymem_models import ExecutionPlan, IdeaSession, ResearchDossier, ResearchFinding
from app.state import db
from core.clock import utcnow


def digest(value) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def identifier(job_id):
    return "pln_" + hashlib.sha256(job_id.encode()).hexdigest()[:32]


def fence(session, payload):
    job = session.get(Job, payload.get("_job_id"), with_for_update=True)
    if not job or job.status != "leased" or job.locked_by != payload.get("_worker_id") or job.attempts != payload.get("_job_attempt"):
        raise FatalError("plan_lease_lost")
    if job.kind != "plan.compile" or (job.workspace_id, job.venture_id) != (payload.get("workspace_id"), payload.get("venture_id")):
        raise FatalError("plan_scope_mismatch")
    from app.worker.main import _job_scope
    _job_scope(SimpleNamespace(kind=job.kind, workspace_id=job.workspace_id, venture_id=job.venture_id, payload=json.loads(job.payload_json)))
    return job


def inputs(session, workspace_id, venture_id, revision_id=None, base_plan_id=None, instruction=None, *, include_builder_runtime=True):
    from app.planning.resources import snapshot
    from app.knowledge.pmf import planning_context
    from app.ventures.current_idea import current_idea, current_revision, revision_fields
    from app.ventures.journey_overrides import NOTE, read as journey_choices
    venture = session.get(Venture, venture_id)
    if not venture or venture.workspace_id != workspace_id:
        raise ValueError("plan_scope_mismatch")
    skipped = journey_choices(venture_id, session=session)["skipped"]
    local = is_local()
    idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture_id,
        IdeaSession.workspace_id == workspace_id, IdeaSession.state != "abandoned")
        .order_by(IdeaSession.updated_at.desc(), IdeaSession.created_at.desc(), IdeaSession.id.desc()).limit(1))
    rev = current_revision(venture, session)
    if revision_id and (not rev or rev.id != revision_id):
        raise ValueError("confirmed_revision_required")
    confirmed = bool(idea and rev and idea.state == "confirmed" and idea.confirmed_revision_id == rev.id)
    if not local and not skipped and not confirmed:
        raise ValueError("confirmed_revision_required")
    current_revision_id = rev.id if rev else None
    gaps = [NOTE] if skipped else []
    if local and not confirmed:
        gaps.append("Your idea has not been reviewed yet. Open Idea to review your audience, problem and offer before executing the plan.")
    dossiers = session.scalars(select(ResearchDossier).where(ResearchDossier.workspace_id == workspace_id,
        ResearchDossier.venture_id == venture_id, ResearchDossier.revision_id == current_revision_id, ResearchDossier.kind == "deep",
        ResearchDossier.state.in_(["complete", "partial"])).order_by(ResearchDossier.created_at.desc())).all()
    dossier = next((row for row in dossiers if json.loads(row.summary or "{}").get("review_kind") in {"model_assessment", "retrieved_sources_only"}), None)
    # Provenance is mandatory; legacy fixture dossiers cannot authorize new plans.
    if not dossier and not skipped and not local:
        raise ValueError("plan_research_required")
    if not dossier:
        gaps.append("No research evidence is saved; this is a provisional plan, not market validation. Open Research and run a scan to check your assumptions.")
    resources = snapshot(workspace_id, venture_id, include_builder_runtime=include_builder_runtime)
    if not resources["inventory"]["complete"] and not skipped and not local:
        raise ValueError("plan_resource_inventory_required")
    if not resources["inventory"]["complete"]:
        gaps.append("Your available tools and time are unknown. Open Plan and add your resources before executing tasks that need them.")
    findings = session.scalars(select(ResearchFinding).where(ResearchFinding.dossier_id == dossier.id,
        ResearchFinding.workspace_id == workspace_id, ResearchFinding.venture_id == venture_id).order_by(ResearchFinding.created_at, ResearchFinding.id).limit(50)).all() if dossier else []
    sources = []
    for row in findings:
        evidence = json.loads(row.evidence or "{}")
        if evidence.get("founder_flag") == "exclude":
            continue
        sources.append({"id": row.id, "title": row.title, "url": row.url, "summary": row.summary[:6000],
            "category": row.category, "observed_at": evidence.get("observed_at"), "license": row.license,
            "license_compatibility": "unverified", "founder_flag": evidence.get("founder_flag")})
    provisional = local and not sources
    if provisional and dossier:
        gaps.append("No usable research sources remain. Open Research and run a scan before treating this provisional plan as evidence-backed.")
    base = None
    base_dependency = None
    if base_plan_id:
        base_row = session.get(ExecutionPlan, base_plan_id)
        if not base_row or (base_row.workspace_id, base_row.venture_id, base_row.revision_id) != (workspace_id, venture_id, current_revision_id):
            raise ValueError("plan_base_scope_mismatch")
        base = json.loads(base_row.structured or "{}").get("proposal")
        if not base:
            raise ValueError("plan_base_unavailable")
        base_dependency = {"id": base_row.id, "proposal_digest": digest(base), "edit_version": json.loads(base_row.structured).get("edit_version")}
    summary = json.loads(dossier.summary or "{}") if dossier else {}
    source_ids = {source["id"] for source in sources}
    review = summary.get("review") or {}
    review = {"overview": review.get("overview"),
        "observations": [item for item in review.get("observations", []) if item.get("source_id") in source_ids],
        "assessments": [item for item in review.get("assessments", []) if set(item.get("source_ids", [])).issubset(source_ids)],
        "service_checks": [item for item in review.get("service_checks", []) if item.get("source_id") in source_ids],
        "proposed_tests": review.get("proposed_tests", [])}
    fields = revision_fields(rev)
    if not fields.get("summary"):
        fields["summary"] = current_idea(venture, session=session)
    if not fields.get("summary"):
        gaps.append("No idea summary is saved; draft a generic checklist and ask for scope before execution.")
    from app.intake.assets import get_site
    context = {"idea": {k: v for k, v in fields.items() if not k.startswith("_")},
        "journey_overrides": {"skipped": skipped},
        "existing_site": get_site(venture_id, session=session).get("site"),
        "sources": sources, "research_gaps": [*summary.get("coverage_gaps", []), *gaps], "research_review": review,
        "assets": resources["inventory"]["assets"], "capabilities": resources["capabilities"],
        "base_plan": base, "founder_instruction": instruction}
    if local:
        context.update(local_mode=True, provisional=provisional)
    from app.gstack.office_hours import brief as gstack_brief
    if idea and rev and (gstack := gstack_brief(session, idea.id, rev.id)):
        context["gstack_brief"] = gstack
    context["precedents"] = planning_context(context["idea"], raw_idea=venture.raw_idea or venture.name)
    from app.knowledge import yc_video_advice
    context["yc_video_advice"] = yc_video_advice.planning_context(context["idea"], raw_idea=venture.raw_idea or venture.name)
    context["managed_publication"] = resources["managed_publication"]
    from app.conversion.waitlist import public_url, status as waitlist_status, VERSION
    if local:
        from app.core.local_secrets import read_settings
        publishing = read_settings("publishing")["values"]
        smtp = read_settings("smtp")["values"]
        context["managed_conversion"] = {"kind": "owner_contact",
            "url": smtp.get("booking_link") or ("mailto:" + publishing["waitlist_email"] if publishing.get("waitlist_email") else None),
            "setup_path": "/settings",
            "requirement": "Use only your saved booking link or waitlist email. If neither is saved, open Keys and connections and add Publishing waitlist email or SMTP booking link before publishing your page."}
        context["managed_conversion_observation"] = {"enabled": False, "submission_verified": False,
            "verified_at": None, "total": 0, "email_verified": 0,
            "note": "No public signup form has been verified. Test your page's saved contact link before publishing."}
    else:
        context["managed_conversion"] = {"kind": "venture_waitlist", "url": public_url(venture_id),
            "consent_version": VERSION, "setup_path": f"/v/{venture_id}/site",
            "verification_steps": ["Open Website.", "Choose Enable waitlist if it is closed.",
                "Choose Test public submission when verification is missing or expired.",
                "Require the Public submission and storage verified result before publication."],
            "test_policy": "The built-in test creates a labelled synthetic receipt, excluded from lead counts. Retain that receipt; do not request a personal email or delete the test entry.",
            "email_confirmation": "No verification email or double opt-in is sent. Recorded interest is not verified email ownership, active usage or payment.",
            "requirement": "Verification expires after 24 hours and closing or changing consent invalidates it. Check current status before publication; never infer demand from test submissions."}
    # This receipt-timestamped snapshot informs prose only. New signups or an expired
    # probe must not invalidate an otherwise reviewed plan; publication checks
    # the authoritative receipt again immediately before provider effects.
        observed = waitlist_status(venture_id, workspace_id)
        context["managed_conversion_observation"] = {
            **{key: observed[key] for key in ("enabled", "submission_verified", "verified_at", "total", "email_verified", "note")},
        }
    from app.memory.task_context import for_task as institutional_memory
    context["institutional_memory"] = institutional_memory(
        session, workspace_id=workspace_id, venture_id=venture_id,
        title="Plan the venture's next work", tags=["planning", "build", "suggestions"])
    dependency = {"revision_id": current_revision_id, "dossier_id": dossier.id if dossier else None, "sources": sources, "inventory": resources["inventory"], "base_plan": base_dependency}
    memory = context["institutional_memory"]
    if memory["availability"] != "available":
        context["research_gaps"].append("Saved memory is unavailable; use current source records and verify missing constraints before execution.")
    # Empty institutional memory must preserve previously approved plan input
    # identity. A substantive correction/forget still changes the dependency.
    if memory["availability"] != "available" or memory["statements"] or memory["omitted_count"]:
        dependency["institutional_memory"] = memory
    if skipped:
        dependency["journey_overrides"] = skipped
        dependency["existing_site"] = context["existing_site"]
    if "gstack_brief" in context:
        dependency["gstack_brief"] = context["gstack_brief"]
    dependency["precedent_corpus"] = context["precedents"]["corpus_sha256"]
    if context["yc_video_advice"]["items"]:
        dependency["yc_video_advice_corpus"] = context["yc_video_advice"]["corpus_sha256"]
    dependency["managed_conversion"] = context["managed_conversion"]
    dependency["managed_publication"] = context["managed_publication"]
    return {"context": context, "resources": resources, "revision_id": current_revision_id, "session_id": idea.id if idea else None,
        "dossier_id": dossier.id if dossier else None, "dependency_digest": digest(dependency), "base_plan_id": base_plan_id}


def claim(payload):
    pid = identifier(payload["_job_id"])
    with db()() as session:
        fence(session, payload)
        # Venture lock serializes version allocation, inventory changes and enqueue.
        session.get(Venture, payload["venture_id"], with_for_update=True)
        row = session.get(ExecutionPlan, pid, with_for_update=True)
        if row is None:
            try:
                data = inputs(session, payload["workspace_id"], payload["venture_id"], payload.get("revision_id"), payload.get("base_plan_id"), payload.get("instruction"))
            except ValueError as exc:
                raise FatalError(str(exc)) from exc
            version = (session.scalar(select(func.max(ExecutionPlan.version)).where(ExecutionPlan.venture_id == payload["venture_id"])) or 0) + 1
            row = ExecutionPlan(id=pid, workspace_id=payload["workspace_id"], venture_id=payload["venture_id"],
                revision_id=data["revision_id"], version=version, state="draft", summary="", target_days=14,
                goal_first_paid_user="", structured=json.dumps({"method": "metered_foundry_plan", "edit_version": 0,
                    "_runtime": {**data, "job_id": payload["_job_id"], "operations": {}, "calls_used": 0}}), created_at=utcnow())
            session.add(row)
        session.commit()
    return pid


def read(pid):
    with db()() as session:
        row = session.get(ExecutionPlan, pid)
        if row is None:
            raise KeyError("plan")
        session.expunge(row)
        return row


def mutate(pid, payload, fn):
    with db()() as session:
        if payload is not None:
            fence(session, payload)
        row = session.get(ExecutionPlan, pid, with_for_update=True)
        doc = json.loads(row.structured)
        result = fn(row, doc, session)
        row.structured = json.dumps(doc, ensure_ascii=False)
        session.commit()
        return result


def before_call(row, doc, session):
    runtime = doc["_runtime"]
    try:
        current = inputs(session, row.workspace_id, row.venture_id, row.revision_id, runtime.get("base_plan_id"))
    except ValueError as exc:
        raise FatalError(str(exc)) from exc
    if current["dependency_digest"] != runtime["dependency_digest"]:
        raise FatalError("plan_inputs_changed")
    if runtime["calls_used"] >= 3:
        raise FatalError("plan_call_budget_exhausted")
    runtime["calls_used"] += 1


journal = OperationJournal(read=read, mutate=mutate, document=lambda pid: json.loads(read(pid).structured),
                           before_call=before_call, prefix="plan")
