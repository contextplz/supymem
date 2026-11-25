"""Metered plan authoring, deterministic scope and explicit review approval.

Approval records the reviewed strategy. Build dispatch and its bounded estimate
are separate contracts and are not implied by a successful plan compilation.
"""
from __future__ import annotations

import json
from datetime import timedelta

from sqlalchemy import select

from app.core.fsm import advance
from app.core.jobs import FatalError, enqueue
from app.core.local_mode import is_local
from app.db.models import Job, PlannedTask, Venture
from app.db.supymem_models import ExecutionPlan, FounderAction, FounderRequest, PlanMilestone, PlanTask, ResourceEstimate
from app.planning import author, execution as ex, request_policy
from app.state import db
from core.clock import as_utc, utcnow
from core.ids import new_id


def _approved_work(plan_id: str, doc: dict, *, completed_task_ids: frozenset[str] = frozenset()) -> list[dict]:
    """Project the founder-reviewed plan into the next-five Work queue."""
    proposal = doc["proposal"]
    entity_ids = doc["entity_ids"]["tasks"]
    work: list[dict] = []
    for milestone in proposal.get("milestones") or []:
        if not milestone.get("included", True):
            continue
        for task in milestone.get("tasks") or []:
            plan_task_id = entity_ids[milestone["key"] + ":" + task["key"]]
            if plan_task_id in completed_task_ids:
                continue
            scope = (doc.get("scope_details") or {}).get(plan_task_id, {}).get("scope")
            acceptance = task.get("acceptance") or []
            detail = str(task.get("description") or "").strip()
            if acceptance:
                detail += "\n\nDone when:\n" + "\n".join(
                    f"- {criterion}" for criterion in acceptance
                )
            if scope == "out":
                reason = (doc.get("scope_details") or {}).get(plan_task_id, {}).get("reason")
                detail += "\n\nUnavailable in this plan. " + (reason or "Review and revise the execution plan before running it.")
            work.append(
                {
                    "title": task["title"],
                    "detail": detail,
                    "why_now": milestone.get("description") or milestone["title"],
                    "tags": [
                        f"plan:{plan_id}",
                        f"plan_task:{plan_task_id}",
                        f"milestone:{milestone.get('kind') or milestone['key']}",
                        f"capability:{task['capability_id']}",
                        f"implementation:{task['implementation']}",
                    ],
                    "est_minutes": 30,
                    # Metered builds are actionable in the desktop app, but
                    # the generic/autopilot runner must never claim them.
                    "runnable": (
                        scope != "out"
                        and
                        task.get("implementation") != "founder_action"
                        and task.get("capability_id") not in {"build.run", "coding.subscription_static", "coding.subscription_product_source"}
                    ),
                    "rank": len(work),
                }
            )
            if len(work) == 5:
                return work
    return work


def _ensure_approved_work(session, row: ExecutionPlan, doc: dict) -> list[dict]:
    """Queue one durable next-five projection for this exact approved plan."""
    source = f"approved_plan:{row.id}"
    existing = session.scalar(
        select(PlannedTask.id).where(
            PlannedTask.venture_id == row.venture_id,
            PlannedTask.source == source,
        ).limit(1)
    )
    if existing:
        return []
    from app.planning.store import save_tasks

    return save_tasks(
        row.venture_id,
        _approved_work(row.id, doc),
        source=source,
        session=session,
        publish=False,
    )


def start(venture_id, workspace_id, *, revision_id=None, base_plan_id=None, instruction=None):
    if instruction is not None and (not instruction.strip() or len(instruction) > 8000):
        raise ValueError("plan_instruction_invalid")
    with db()() as session:
        venture = session.get(Venture, venture_id, with_for_update=True)
        if not venture or venture.workspace_id != workspace_id:
            raise ValueError("plan_scope_mismatch")
        data = ex.inputs(session, workspace_id, venture_id, revision_id, base_plan_id, instruction)
        if is_local() and next(item for item in data["resources"]["capabilities"] if item["id"] == "plan.compile")["state"] != "implemented":
            raise ValueError("plan_model_required")
        active = session.scalars(select(Job).where(Job.workspace_id == workspace_id, Job.venture_id == venture_id,
            Job.kind == "plan.compile", Job.status.in_(["scheduled", "leased", "retry_wait"]))).all()
        for job in active:
            old = json.loads(job.payload_json)
            if (old.get("revision_id"), old.get("base_plan_id"), old.get("instruction")) == (data["revision_id"], base_plan_id, instruction):
                return {"job_id": job.id}
        jid = enqueue(session, "plan.compile", {"workspace_id": workspace_id, "venture_id": venture_id,
            "revision_id": data["revision_id"], "base_plan_id": base_plan_id, "instruction": instruction},
            workspace_id=workspace_id, venture_id=venture_id)
        session.commit()
    __import__("app.experiments.ledger", fromlist=["x"]).on_research_accepted(venture_id, data)  # experiment ledger
    return {"job_id": jid}


def _project(context):
    from copy import deepcopy
    result = deepcopy(context)
    source_map = {f"source_{i+1}": row["id"] for i, row in enumerate(result["sources"])}
    asset_map = {f"resource_{i+1}": row["id"] for i, row in enumerate(result["assets"])}
    for label, row in zip(source_map, result["sources"], strict=True):
        row["id"] = label
    for label, row in zip(asset_map, result["assets"], strict=True):
        row["id"] = label
    reverse = {value: key for key, value in source_map.items()}
    from app.research.citations import restore
    if result.get("research_review"):
        result["research_review"] = restore(result["research_review"], reverse)
    if result.get("base_plan"):
        resources = {value: key for key, value in asset_map.items()}
        for milestone in result["base_plan"]["milestones"]:
            for task in milestone["tasks"]:
                task["source_ids"] = [reverse[sid] for sid in task["source_ids"] if sid in reverse]
                task["resource_ids"] = [resources[rid] for rid in task["resource_ids"] if rid in resources]
    return result, source_map, asset_map


def generation_quote(venture_id, workspace_id, *, base_plan_id=None, instruction=None):
    if instruction is not None and (not instruction.strip() or len(instruction) > 8000):
        raise ValueError("plan_instruction_invalid")
    from app.llm.founder_provider import FounderLLMConfigError, resolve_llm_config
    from app.credits.units import MICRO_PER_CREDIT
    with db()() as session:
        data = ex.inputs(session, workspace_id, venture_id, base_plan_id=base_plan_id, instruction=instruction)
    context, _, _ = _project(data["context"])
    request = request_policy.prepare(context)
    try:
        configuration = resolve_llm_config(workspace_id)
    except FounderLLMConfigError:
        if not is_local():
            raise
        raise ValueError("plan_model_required") from None
    founder = configuration.source == "founder_override"
    if is_local() and not configuration.key_is_set:
        raise ValueError("plan_model_required")
    estimate = 0 if is_local() and founder else request.estimate(configuration.model)
    credits = data["resources"]["credit_account"]
    ceiling = 0 if founder else estimate
    return {"provider": configuration.provider, "model": configuration.model,
        "payer": "founder" if founder else "platform_credits", "max_output_tokens": request.max_output_tokens,
        "generation_policy": request_policy.public_policy(),
        "ceiling_micro": ceiling, "ceiling_credits": ceiling / MICRO_PER_CREDIT,
        "available_micro": credits["available_micro"],
        "sufficient_now": founder or (credits["state"] == "active" and credits["available_micro"] is not None and credits["available_micro"] >= ceiling),
        "basis": "Configured model rates, prompt token estimate and maximum output; known provider usage settles up to the reserved ceiling. An uncertain outcome retains its reservation.",
        "execution_costs_included": False}


def run_compile(payload):
    from app.research.metered import synthesize
    pid = ex.claim(payload)
    row = ex.read(pid)
    doc = json.loads(row.structured)
    if doc.get("proposal"):
        return get_plan(pid)
    context, source_map, asset_map = _project(doc["_runtime"]["context"])
    request = request_policy.prepare(context)
    def parse(raw):
        value = author.parse(raw, context)
        # A target duration is a founder-set goal, never a provider estimate.
        value["target_days"] = (context.get("base_plan") or {}).get("target_days")
        for milestone in value["milestones"]:
            for task in milestone["tasks"]:
                task["source_ids"] = [source_map[sid] for sid in task["source_ids"]]
                task["resource_ids"] = [asset_map[rid] for rid in task["resource_ids"]]
        return value
    try:
        proposal = synthesize(pid, payload, messages=request.messages, schema=request.schema, parse=parse,
            purpose=request_policy.PURPOSE, key="plan", journal=ex.journal, prefix="plan", max_tokens=request.max_output_tokens,
            require_stop=True, cap_at_reservation=True, pin_provider=True, repair=True,
            provider_timeout_seconds=request.provider_timeout_seconds)
    except FatalError as error:
        if str(error) not in {"plan_invalid_output", "plan_output_truncated", "plan_outcome_uncertain"}:
            raise
        ex.mutate(pid, payload, lambda row, doc, session: doc["_runtime"].update(generation_failed=True))
        return get_plan(pid)
    _finish(pid, payload, proposal)
    return get_plan(pid)


def compile_plan(venture_id, workspace_id, revision_id, *, operation_id=None):
    """Compatibility entry: reading a committed result is safe; new work queues."""
    if operation_id:
        try:
            row = ex.read(ex.identifier(operation_id))
        except KeyError:
            pass
        else:
            if (row.workspace_id, row.venture_id, row.revision_id) != (workspace_id, venture_id, revision_id):
                raise ValueError("plan_scope_mismatch")
            if json.loads(row.structured).get("proposal"):
                return get_plan(row.id)
    return start(venture_id, workspace_id, revision_id=revision_id)


def _task_scope(task, capabilities, milestone_kind=None):
    capability = capabilities.get(task["capability_id"], {})
    if task["capability_id"] == "founder.action":
        return "conditional", "Founder action and approval required."
    if task.get("implementation") == "founder_action":
        state = "conditional" if capability.get("state") == "implemented" else "out"
        return state, "A manual founder fallback is proposed. Supymem execution is not authorized for this task."
    if capability.get("state") != "implemented":
        return "out", capability.get("reason") or "This hosted executor is not integrated."
    supported_kinds = capability.get("supported_milestone_kinds")
    if supported_kinds and milestone_kind is not None and milestone_kind not in supported_kinds:
        return "out", (
            f"{task['capability_id']} is not available for {milestone_kind} milestones; "
            f"it currently supports: {', '.join(supported_kinds)}."
        )
    return "conditional", "A bounded operation and current authorization are required before execution."


def _persist(session, row, doc, proposal):
    """Apply an authored or manually edited proposal atomically, preserving IDs."""
    capabilities = {c["id"]: c for c in doc["_runtime"]["context"]["capabilities"]}
    due_at = utcnow() + timedelta(days=proposal.get("target_days") or 14)
    previous = doc.get("entity_ids", {})
    entity_ids = {"milestones": {}, "tasks": {}}
    old_miles = session.scalars(select(PlanMilestone).where(PlanMilestone.plan_id == row.id)).all()
    old_tasks = session.scalars(select(PlanTask).where(PlanTask.milestone_id.in_([m.id for m in old_miles]))).all() if old_miles else []
    if old_tasks:
        actions = session.scalars(select(FounderAction).where(FounderAction.plan_task_id.in_([t.id for t in old_tasks]))).all()
        for action in actions:
            session.delete(action)
        session.flush()
    for task in old_tasks:
        session.delete(task)
    session.flush()
    for milestone in old_miles:
        session.delete(milestone)
    session.flush()
    for milestone in proposal["milestones"]:
        entity_ids["milestones"][milestone["key"]] = previous.get("milestones", {}).get(milestone["key"]) or new_id("mls")
    scope_details = {}
    for index, milestone in enumerate(proposal["milestones"]):
        task_states = [
            _task_scope(task, capabilities, milestone["kind"])
            for task in milestone["tasks"]
        ]
        scope = "out" if not milestone["included"] or any(state == "out" for state, _ in task_states) else "conditional"
        mid = entity_ids["milestones"][milestone["key"]]
        session.add(PlanMilestone(id=mid, plan_id=row.id, workspace_id=row.workspace_id, venture_id=row.venture_id,
            order=index + 1, title=milestone["title"], description=milestone["description"], kind=milestone["kind"], scope=scope,
            state="pending", estimate_credits=0, depends_on=json.dumps([entity_ids["milestones"][key] for key in milestone["depends_on"]]),
            acceptance=json.dumps(milestone["acceptance"])))
        session.flush()
        for order, (task, state) in enumerate(zip(milestone["tasks"], task_states, strict=True)):
            key = milestone["key"] + ":" + task["key"]
            tid = previous.get("tasks", {}).get(key) or new_id("tsk")
            entity_ids["tasks"][key] = tid
            scope_details[tid] = {"scope": state[0], "reason": state[1], "approval": "founder", "execution_ready": False}
            session.add(PlanTask(id=tid, milestone_id=mid, workspace_id=row.workspace_id, venture_id=row.venture_id,
                order=order + 1, title=task["title"], description=task["description"], agent_role="founder" if task["capability_id"] == "founder.action" else "mini_swe",
                tool_hints=json.dumps([task["capability_id"]]), state="pending", estimate_credits=0, created_at=utcnow(), due_at=due_at))
            if task["founder_action"]:
                action = task["founder_action"]
                session.add(FounderAction(id=new_id("fact"), workspace_id=row.workspace_id, venture_id=row.venture_id,
                    plan_task_id=tid, title=action["title"], steps=json.dumps(action["steps"] + ["Return evidence: " + action["verification"]]),
                    required_secret_slots="[]", state="open", blocks_task=True, created_at=utcnow()))
    doc.update(proposal=proposal, entity_ids=entity_ids, scope_details=scope_details, edit_version=doc.get("edit_version", 0) + 1)
    row.summary, row.goal_first_paid_user = proposal["summary"], proposal["goal_first_paid_user"]
    if doc["_runtime"]["context"].get("provisional") and not row.summary.startswith("Provisional plan:"):
        row.summary = "Provisional plan: " + row.summary
    # Legacy column is nonnullable. Public target_days comes from the proposal.
    row.target_days = proposal["target_days"] or 14
    estimate = doc["_runtime"]["resources"]["execution_budget"]
    session.add(ResourceEstimate(id=new_id("est"), plan_id=row.id, workspace_id=row.workspace_id,
        venture_id=row.venture_id, version=doc["edit_version"], breakdown=json.dumps(estimate),
        gross_upfront_credits=0, total_upfront_credits=0, monthly_run_credits=0, confidence="unknown",
        method_notes=json.dumps({"numeric_columns_unpriced": True, "source": "runtime_capability_assessment", "no_asset_discount": True}), created_at=utcnow()))


def _finish(pid, payload, proposal):
    def apply(row, doc, session):
        if doc.get("proposal"):
            return
        try:
            current = ex.inputs(session, row.workspace_id, row.venture_id, row.revision_id, doc["_runtime"].get("base_plan_id"))
        except ValueError as exc:
            raise FatalError("plan_inputs_changed") from exc
        if current["dependency_digest"] != doc["_runtime"]["dependency_digest"]:
            raise FatalError("plan_inputs_changed")
        from app.planning.review import repair_prices
        repair_prices(proposal, doc["_runtime"]["context"])
        proposal_valid = author.validate(proposal, doc["_runtime"]["context"])
        _persist(session, row, doc, proposal_valid)
        advance(session, row, "founder_review")
    ex.mutate(pid, payload, apply)


def _current(row, doc, session, *, include_builder_runtime=True):
    try:
        live = ex.inputs(session, row.workspace_id, row.venture_id, row.revision_id, doc.get("_runtime", {}).get("base_plan_id"), include_builder_runtime=include_builder_runtime)
        stale = live["dependency_digest"] != doc.get("_runtime", {}).get("dependency_digest")
        return live, stale
    except ValueError:
        return None, True


def get_plan(plan_id):
    with db()() as session:
        row = session.get(ExecutionPlan, plan_id)
        if not row:
            raise KeyError("plan")
        doc = json.loads(row.structured or "{}")
        runtime = doc.get("_runtime", {})
        proposal = doc.get("proposal")
        from app.planning.review import findings as review_findings
        corrections = review_findings(proposal, runtime.get("context", {}))
        live, stale = _current(row, doc, session)
        operations = runtime.get("operations", {})
        usage = {"credits_micro": sum(op.get("usage", {}).get("credits_micro", 0) for op in operations.values()),
            "has_unsettled": any(op.get("state") in {"calling", "responded", "uncertain"} for op in operations.values()),
            "operations": [{"state": op.get("state"), "provider": op.get("usage", {}).get("provider"),
                "model": op.get("usage", {}).get("model"), "credits_micro": op.get("usage", {}).get("credits_micro"),
                "source": op.get("usage", {}).get("source"), "error_code": op.get("error_code")} for op in operations.values()]}
        milestones = []
        from app.planning.publication_evidence import public_site_evidence
        publication = public_site_evidence(session, workspace_id=row.workspace_id,
                                           venture_id=row.venture_id) if any(
            milestone.get("kind") == "landing" for milestone in (proposal or {}).get("milestones", [])) else None
        for milestone in (proposal or {}).get("milestones", []):
            mid = doc["entity_ids"]["milestones"][milestone["key"]]
            model = session.get(PlanMilestone, mid)
            tasks = []
            for task in milestone["tasks"]:
                tid = doc["entity_ids"]["tasks"][milestone["key"] + ":" + task["key"]]
                task_row = session.get(PlanTask, tid)
                tasks.append({**task, "id": tid, "state": task_row.state if task_row else "unavailable", "estimate_credits": None,
                    "due_at": as_utc(task_row.due_at).isoformat() if task_row and task_row.due_at else None,
                    "deadline_is_estimate": True, **doc["scope_details"][tid]})
            milestones.append({**milestone, "id": mid, "order": model.order, "scope": model.scope, "state": model.state,
                "public_site_evidence": publication if milestone.get("kind") == "landing" else None,
                "estimate_credits": None, "tasks": tasks, "depends_on_ids": json.loads(model.depends_on)})
        resources = live["resources"] if live else runtime.get("resources")
        estimate = (resources or {}).get("execution_budget", {"status": "unknown", "upfront_credits": None, "monthly_credits": None})
        newer = session.scalar(select(ExecutionPlan.id).where(ExecutionPlan.venture_id == row.venture_id,
            ExecutionPlan.version > row.version, ExecutionPlan.state.in_(["founder_review", "approved"])).limit(1))
        token = ex.digest({"id": row.id, "proposal": proposal, "edit_version": doc.get("edit_version", 0),
                           "dependencies": live["dependency_digest"] if live else None})
        job = session.get(Job, runtime.get("job_id")) if runtime.get("job_id") else None
        generation_state = "completed" if proposal else "failed" if runtime.get("generation_failed") or job and job.status in {"failed_dead", "cancelled"} else "running" if job else "unverified"
        recovery = None
        if generation_state == "failed":
            recovery = ({"label": "Review saved research", "href": f"/v/{row.venture_id}/research",
                         "message": "Planning did not finish and its charge is still being checked. No repeat call was made. Use your saved research to choose a manual next step while the charge is resolved."}
                        if usage["has_unsettled"] else
                        {"label": "Retry plan", "href": f"/v/{row.venture_id}/plan#plan-create",
                         "message": "The proposed plan did not pass review. Your idea and research are saved. Review a new planning estimate and approve it to try again; completed model usage remains charged."})
            if is_local():
                recovery["message"] = ("Your model call has an uncertain result. Open Research and review your saved sources while its usage is checked; do not start a repeat call."
                    if usage["has_unsettled"] else "Your plan did not finish. Your saved idea and research are kept. Open Plan and choose Generate plan to retry with your saved model key.")
        research_gaps = runtime.get("context", {}).get("research_gaps", [])
        limitation = ("You choose which tasks to run. Save any required keys in Keys and connections before running those tasks."
            if is_local() else "Execution requires an integrated build provider, a bounded budget and separate execution approval.")
        return {"id": row.id, "workspace_id": row.workspace_id, "venture_id": row.venture_id, "revision_id": row.revision_id,
            "provisional": bool(runtime.get("context", {}).get("provisional")),
            "version": row.version, "edit_version": doc.get("edit_version", 0), "review_token": token,
            "state": row.state, "generation_state": generation_state, "job_id": runtime.get("job_id"), "dossier_id": runtime.get("dossier_id"),
            "generation_recovery": recovery,
            "summary": row.summary, "goal_first_paid_user": row.goal_first_paid_user, "target_days": (proposal or {}).get("target_days"),
            "method": doc.get("method", "legacy_unverified"), "execution_ready": False, "stale_dependencies": stale,
            "reviewable": row.state == "founder_review" and not stale and not newer, "newer_plan_id": newer,
            "review_findings": corrections,
            "conversion_observation": runtime.get("context", {}).get("managed_conversion_observation"),
            "limitations": [limitation] + research_gaps + (["Your idea, research or resources changed. Open Plan and regenerate before approving this draft."] if stale else []) + ([recovery["message"]] if recovery else []),
            "milestones": milestones, "proposal": proposal, "resources": resources,
            "precedents": runtime.get("context", {}).get("precedents"),
            "assumptions": (proposal or {}).get("assumptions", []), "exclusions": (proposal or {}).get("exclusions", []),
            "evidence_gaps": list(dict.fromkeys([*(proposal or {}).get("evidence_gaps", []), *(research_gaps if is_local() else [])])), "sources": runtime.get("context", {}).get("sources", []),
            "out_of_scope": [m["title"] for m in milestones if m["scope"] == "out"],
            "conditional": [m["title"] for m in milestones if m["scope"] == "conditional"],
            "estimate": {**estimate, "total_upfront_credits": estimate.get("upfront_credits"), "monthly_run_credits": estimate.get("monthly_credits"), "confidence": "unknown"},
            "feasibility": {"ok": None, "status": "local" if is_local() else "unknown", "shortfall": None, "options": [], "explanation": "You use your own model account. Open Keys and connections to set your monthly spend cap." if is_local() else "Execution costs are unpriced until the required executors and services are verified."},
            "usage": usage, "approved_at": row.approved_at.isoformat() if row.approved_at else None}


def list_plans(venture_id):
    with db()() as session:
        rows = session.scalars(select(ExecutionPlan).where(ExecutionPlan.venture_id == venture_id).order_by(ExecutionPlan.version.desc()).limit(100)).all()
        return [{"id": row.id, "version": row.version, "state": row.state, "summary": row.summary,
                 "job_id": json.loads(row.structured or "{}").get("_runtime", {}).get("job_id")} for row in rows]


def patch_plan(plan_id, *, edit_version, patch):
    allowed = {"summary", "goal_first_paid_user", "target_days", "assumptions", "exclusions", "evidence_gaps", "milestones", "precedent_applications"}
    if not patch or not set(patch).issubset(allowed):
        raise ValueError("invalid_plan_patch")
    def apply(row, doc, session):
        if row.state != "founder_review" or not doc.get("proposal"):
            raise ValueError("plan_not_editable")
        if edit_version != doc.get("edit_version"):
            raise ValueError("plan_edit_conflict")
        newer = session.scalar(select(ExecutionPlan.id).where(ExecutionPlan.venture_id == row.venture_id,
            ExecutionPlan.version > row.version, ExecutionPlan.state.in_(["founder_review", "approved"])).limit(1))
        if newer:
            raise ValueError("plan_version_superseded")
        proposal = author.validate({**doc["proposal"], **patch}, doc["_runtime"]["context"])
        before = json.dumps(doc["proposal"], ensure_ascii=False)
        from app.tenancy import current_or_none
        scope = current_or_none()
        actor = scope.actor if scope else "system"
        advance(session, row, "editing", actor=actor, reason="founder_manual_edit")
        _persist(session, row, doc, proposal)
        session.add(FounderRequest(id=new_id("frq"), workspace_id=row.workspace_id, venture_id=row.venture_id,
            source="plan_manual_edit", text=json.dumps({"actor": actor, "from_version": edit_version,
                "to_version": doc["edit_version"], "before": json.loads(before), "after": proposal}, ensure_ascii=False),
            intent="plan_edit", intent_category="planning", classification="manual_review", outcome="applied",
            related_id=row.id, created_at=utcnow(), resolved_at=utcnow()))
        advance(session, row, "founder_review", actor=actor, reason="founder_manual_edit_saved")
    ex.mutate(plan_id, None, apply)
    return get_plan(plan_id)


def _approval_deadlines(session, row, doc, *, first_approval=False):
    """Anchor estimates once; repeated approval never postpones saved deadlines."""
    due_at = as_utc(row.approved_at or row.created_at) + timedelta(days=(doc.get("proposal") or {}).get("target_days") or 14)
    query = select(PlanTask).join(PlanMilestone, PlanMilestone.id == PlanTask.milestone_id).where(
        PlanMilestone.plan_id == row.id, PlanMilestone.workspace_id == row.workspace_id,
        PlanMilestone.venture_id == row.venture_id, PlanTask.workspace_id == row.workspace_id,
        PlanTask.venture_id == row.venture_id)
    if not first_approval:
        query = query.where(PlanTask.due_at.is_(None))
    for task in session.scalars(query):
        task.due_at = due_at


def approve_plan(plan_id, *, person_id, review_token):
    from app.core.stages import reach_at_least
    from app.authz.roles import role_allows
    from app.authz.workspace import subject_person_id
    from app.workspaces.service import role_of
    from core.enums import VentureStage
    person_id = subject_person_id(person_id)
    queued_work: list[dict] = []
    work_venture_id = ""
    first_approval = False
    work_workspace_id = ""
    with db()() as session:
        snapshot = session.get(ExecutionPlan, plan_id)
        if not snapshot:
            raise KeyError("plan")
        venture = session.get(Venture, snapshot.venture_id, with_for_update=True)
        row = session.scalar(select(ExecutionPlan).where(ExecutionPlan.id == plan_id).with_for_update().execution_options(populate_existing=True))
        if not role_allows(role_of(row.workspace_id, person_id), "approve"):
            raise PermissionError("plan_approval_forbidden")
        doc = json.loads(row.structured)
        work_venture_id = row.venture_id
        live, stale = _current(row, doc, session)
        token = ex.digest({"id": row.id, "proposal": doc.get("proposal"), "edit_version": doc.get("edit_version", 0),
                           "dependencies": live["dependency_digest"] if live else None})
        if stale:
            raise ValueError("plan_inputs_changed")
        if token != review_token:
            raise ValueError("plan_review_conflict")
        if row.state == "approved":
            _approval_deadlines(session, row, doc)
            venture.stage = reach_at_least(venture.stage, VentureStage.BUILDING).value
            queued_work = _ensure_approved_work(session, row, doc)
            session.commit()
        else:
            from app.planning.review import findings as review_findings
            if review_findings(doc.get("proposal"), doc.get("_runtime", {}).get("context", {})):
                raise ValueError("plan_claim_review_required")
            if row.state != "founder_review" or not doc.get("proposal"):
                raise ValueError("plan_not_reviewable")
            newer = session.scalar(select(ExecutionPlan.id).where(ExecutionPlan.venture_id == row.venture_id,
                ExecutionPlan.version > row.version, ExecutionPlan.state.in_(["founder_review", "approved"])).limit(1))
            if newer:
                raise ValueError("plan_version_superseded")
            advance(session, row, "approved", actor=person_id, reason="founder_review_approved", meta={"review_token": review_token, "execution_started": False})
            row.approved_at, row.approved_by = utcnow(), person_id
            _approval_deadlines(session, row, doc, first_approval=True)
            first_approval = True
            work_workspace_id = row.workspace_id
            venture.stage = reach_at_least(venture.stage, VentureStage.BUILDING).value
            previous = session.scalars(select(ExecutionPlan).where(ExecutionPlan.venture_id == row.venture_id,
                ExecutionPlan.id != row.id, ExecutionPlan.state == "approved").with_for_update()).all()
            for base in previous:
                advance(session, base, "superseded", actor=person_id, reason="replacement_plan_approved")
                base.superseded_by = row.id
            queued_work = _ensure_approved_work(session, row, doc)
            session.commit()
    if first_approval:
        from app.analytics.events import emit
        emit("plan.approved", person_id=person_id, workspace_id=work_workspace_id,
             venture_id=work_venture_id, props={"plan_id": plan_id})
    if queued_work:
        from app.planning.store import publish_saved_tasks

        publish_saved_tasks(work_venture_id, queued_work)
    return {"ok": True, "execution_started": False, "plan": get_plan(plan_id)}


def edit_intent(text):
    if not text.strip() or len(text) > 8000:
        raise ValueError("plan_instruction_invalid")
    return {"operation": "revise_plan", "args": {"text": text}, "preview_diff": [],
            "requires_generation": True, "message": "Submit this instruction to generate a new metered draft for review."}


def apply_edit(plan_id, operation, args):
    row = ex.read(plan_id)
    if operation == "revise_plan":
        return start(row.venture_id, row.workspace_id, revision_id=row.revision_id, base_plan_id=plan_id, instruction=str(args.get("text") or ""))
    if operation == "patch_plan":
        return patch_plan(plan_id, edit_version=args.get("edit_version"), patch=args.get("patch") or {})
    raise ValueError("unsupported_plan_operation")
