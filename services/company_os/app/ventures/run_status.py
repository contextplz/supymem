"""Read-only founder status projection; never starts work or invents progress."""
from __future__ import annotations

import json
import re
from datetime import datetime
from urllib.parse import urlencode

from sqlalchemy import case, func, select
from core.clock import as_utc, utcnow
from app.db.models import Job, PlannedTask, Venture, VentureDocument, Workspace
from app.db.task_models import Task, TaskRun, TaskStep
from app.db.supymem_models import AppRuntimeIntent, BuildRun, ClarifierTurn, ExecutionPlan, IdeaRevision, IdeaSession, ResearchDossier
from app.state import db
from app.ventures.next_step import current_research_report, validation_settled
from app.ventures.device_attention import pending_mac_reconciliation
from app.research.execution import identifier as research_identifier
from app.planning.execution import identifier as plan_identifier
from app.planning.publication_evidence import landing_build_result, landing_publication_for_task
from app.planning.store import current_plan_rows

ACTIVE = ("scheduled", "pending", "leased", "running", "retry_wait")
FAILED = ("failed", "failed_dead", "poison")
# Label and exact destination of the workflow that owns each durable job.
WORK = {
    "experiment.exploration.prepare": ("Customer ideas", "Preparing customer ideas", "now"),
    "gtm.god.run": ("Go-to-market diagnosis", "Preparing your go-to-market diagnosis", "now"),
    "content.draft": ("Content draft", "Writing a content draft", "now"),
    "content.topics": ("Content ideas", "Finding content ideas", "now"),
    "task.run": ("Company task", "Running a saved company task", "tasks"),
    "tasks.execute": ("Your task", "Advancing the saved task plan", "now"),
    "idea.clarify_turn": ("Clarification", "Preparing your next question", "idea#journey-action"),
    "idea.validate": ("Validation", "Challenging the idea and its assumptions", "idea#journey-action"),
    "research.deep": ("Research", "Gathering and assessing evidence", "research"),
    "plan.compile": ("Planning", "Preparing the execution plan", "plan"),
    "build.run": ("Building", "Building the approved product", "builds"),
    "build.watch": ("Build verification", "Checking build, QA and cleanup receipts", "builds"),
    "qa.run": ("Testing", "Checking the built product", "builds"),
    "deploy.run": ("Deploying", "Publishing the accepted product", "site"),
    "gtm.journey.prepare": ("Find customers", "Finding your first customers", "/gtm"),
    "pagebuilder.build": ("Landing page", "Building your approved landing page", "site"),
}

# One clear next action after a failure (never just "See error"). Planning is optional
# when the one-click page builder is on, so a failed plan points at the page build.
FAILED_ACTION = {
    "experiment.exploration.prepare": ("Retry", "now"),
    "research.deep": ("Run research again", "research"),
    "idea.validate": ("Retry validation", "idea#journey-action"),
    "idea.clarify_turn": ("Continue clarification", "idea#journey-action"),
    "plan.compile": ("Retry plan", "plan"),
    "pagebuilder.build": ("Build my landing page again", "site?build=1"),
    "build.run": ("Review build and retry", "builds"),
    "build.watch": ("Review build and retry", "builds"),
    "qa.run": ("Review build and retry", "builds"),
    "deploy.run": ("Publish again", "site"),
}


def _failed_action(kind: str, base: str) -> dict:
    from app.pagebuilder.api import enabled as page_builder_enabled

    label, route = FAILED_ACTION.get(kind, ("Open and retry", WORK.get(kind, ("", "", "now"))[2]))
    if kind == "plan.compile" and page_builder_enabled():
        label, route = "Build your landing page", "site?build=1"
    return {"label": label, "href": route if route.startswith("/") else f"{base}/{route}"}


def _job_handoff(session, job, venture_id):
    run_id = session.scalar(select(TaskStep.run_id).where(TaskStep.job_id == job.id))
    if not run_id and job.kind == "tasks.execute":
        try:
            payload = json.loads(job.payload_json or "{}")
            run_id = payload.get("task_run_id") if isinstance(payload, dict) else None
        except (ValueError, TypeError):
            return None
    if not run_id:
        return None
    return session.execute(select(Task, TaskRun).join(TaskRun, TaskRun.task_id == Task.id)
        .join(Workspace, Workspace.id == Task.workspace_id).join(Venture, Venture.id == Task.venture_id)
        .where(TaskRun.id == run_id, Task.venture_id == venture_id, Task.workspace_id == job.workspace_id,
               Venture.workspace_id == job.workspace_id, Task.owner_person_id == Workspace.owner_id)).first()


def _background_card(session, job, venture_id):
    from app.db.supymem_models import IdeaClarificationRun
    if job.kind == "idea.clarify_turn" and session.scalar(select(IdeaClarificationRun.id).where(
            IdeaClarificationRun.job_id == job.id, IdeaClarificationRun.workspace_id == job.workspace_id,
            IdeaClarificationRun.venture_id == venture_id,
            IdeaClarificationRun.state.in_(("completed", "superseded")))):
        return None
    from app.org.characters import decode_assignee
    from app.org.manager import manager_name

    label, activity, route = WORK.get(job.kind, ("Background work", "Processing saved company work", "now"))
    owner = {"owner_name": manager_name(venture_id), "owner_role": "Manager"}
    handoff = _job_handoff(session, job, venture_id)
    if handoff:
        task, run = handoff
        if task.state not in {"submitted", "working"} or run.state not in {"submitted", "working"}:
            if job.kind == "pagebuilder.build" and job.status in ACTIVE and _superseded_handoff(session, task):
                handoff = None
            else:
                return None
        else:
            label, activity, route = "Your task", task.title, "now"
    if job.kind == "task.run":
        try:
            payload = json.loads(job.payload_json or "{}")
            task = session.get(PlannedTask, payload.get("task_id")) if isinstance(payload, dict) else None
            if task and task.venture_id == venture_id and task.workspace_id == job.workspace_id:
                activity = task.title
                owner = decode_assignee(json.loads(task.tags_json or "[]")) or owner
        except (ValueError, TypeError):
            pass
    return {"job_id": job.id, "stage": label, "activity": activity,
            "state": "queued" if job.status in {"scheduled", "pending", "retry_wait"} else "working",
            "status": job.status, "href": route if route.startswith("/") else f"/v/{venture_id}/{route}",
            "changed_at": as_utc(job.started_at or job.created_at).isoformat(),
            **({"deadline_at": as_utc(handoff[1].deadline_at).isoformat(), "deadline_is_estimate": True} if handoff else {}),
            **{key: owner.get(key) for key in ("owner_name", "owner_role")}}


def _superseded_handoff(session, task) -> bool:
    """A replacement result retires an admitted job's banner, not its usage hold."""
    if task.kind != "one_time":
        return False
    run = session.scalar(select(TaskRun).where(TaskRun.task_id == task.id, TaskRun.state != "queued")
                         .order_by(TaskRun.started_at.desc(), TaskRun.id.desc()).limit(1))
    steps = session.scalars(select(TaskStep).where(TaskStep.run_id == run.id)).all() if run else []
    if len(steps) != 1 or steps[0].job_id != run.run_key:
        return False
    job = session.get(Job, steps[0].job_id)
    if (not job or (job.workspace_id, job.venture_id, job.kind)
            != (task.workspace_id, task.venture_id, steps[0].skill)):
        return False
    if job.kind == "pagebuilder.build" and job.status in ACTIVE:
        return True
    reason = json.loads(task.blocker_json or "{}")
    stale_gtm = (job.kind == "gtm.journey.prepare" and task.state == "input_required"
                 and reason.get("class") == "needs_input")
    if job.status not in FAILED and not (job.kind == "research.deep" and task.state == "input_required") and not stale_gtm:
        return False
    successors = session.scalars(select(Job).where(
        Job.workspace_id == job.workspace_id, Job.venture_id == job.venture_id, Job.kind == job.kind,
        Job.created_at > job.created_at, Job.status.in_(("succeeded", "partial", "done") + (ACTIVE if job.kind == "pagebuilder.build" else ())))
        .order_by(Job.created_at.desc(), Job.id.desc()))
    for successor in successors:
        if stale_gtm and as_utc(task.updated_at) >= as_utc(successor.finished_at or successor.created_at):
            continue
        if successor.kind == "pagebuilder.build" and successor.status in ACTIVE:
            try:
                revision = json.loads(job.payload_json).get("revision_id")
                if revision and revision == json.loads(successor.payload_json).get("revision_id"):
                    return True
            except (ValueError, TypeError, AttributeError):
                continue
        if job.kind == "research.deep":
            report = session.scalar(select(ResearchDossier).where(
                ResearchDossier.id == research_identifier(successor.id),
                ResearchDossier.workspace_id == job.workspace_id, ResearchDossier.venture_id == job.venture_id,
                ResearchDossier.kind == "deep", ResearchDossier.state.in_(("complete", "completed", "partial")),
                ResearchDossier.completed_at.is_not(None)))
            from app.ventures.next_step import research_report_usable
            if research_report_usable(session, report):
                return True
        else:
            from app.tasks.skills import verify_result
            try:
                payload = json.loads(successor.payload_json)
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict) and verify_result(session, job.kind, {**payload, "_job_id": successor.id},
                    workspace_id=job.workspace_id, venture_id=job.venture_id)["verified"]:
                return True
    return False


def _project_handoffs(session, result, venture_id, workspace_id, report=None):
    from app.tasks import service

    attention = {"input_required", "auth_required", "approval_required"}
    candidates = session.scalars(select(Task).join(Workspace, Workspace.id == Task.workspace_id)
        .join(Venture, Venture.id == Task.venture_id)
        .where(Task.venture_id == venture_id, Task.workspace_id == workspace_id,
               Venture.workspace_id == workspace_id, Task.owner_person_id == Workspace.owner_id)
        .order_by(case((Task.state.in_(attention), 0), else_=1), Task.updated_at.desc(), Task.id.desc())).yield_per(20)
    rows = []
    for candidate in candidates:
        if candidate.state in attention | {"failed", "rejected"} and _superseded_handoff(session, candidate):
            continue
        if report and _superseded_research_task(candidate, report):
            continue
        rows.append(candidate)
        if len(rows) == 50:
            break
    task = next((row for row in rows if row.state in attention), None)
    if task is None:
        task = next((row for row in rows if row.state == "working"), None)
    if task is None:
        task = next((row for row in rows if row.state == "submitted" and row.kind == "one_time" and row.last_run_at), None)
    if task is None:
        if result["state"] in {"queued", "working", "needs_you", "failed"}:
            return None
        task = rows[0] if rows else None
        if task is None or (task.state in service.TERMINAL and not _newer_than_result(task.updated_at, result)):
            return None
    view = service.view(session, task, timeline=False)
    result.update(stage="Your task", changed_at=as_utc(task.updated_at).isoformat(), job_id=None,
                  action=view["action"], deadline_at=view["deadline_at"], deadline_is_estimate=True)
    if view.get("research_recovery"):
        result["secondary_action"] = {"label": "Skip for now", "href": view["action"]["href"]}
    if task.state in attention:
        reason = view["blocker"] or {}
        result.update(state="needs_you", activity=reason.get("reason") or f"{task.title} needs your attention.",
                      continues_when_closed=False)
        return reason.get("unblock_action") or view["action"]["label"]
    if task.state == "working":
        try:
            specs = json.loads(task.scope_json).get("steps", [])
            server_only = all(isinstance(spec, dict) and spec.get("runs_on", "server") != "device" for spec in specs)
        except (ValueError, TypeError, AttributeError):
            server_only = False
        queued = bool(view["steps"]) and all(step["state"] == "submitted" for step in view["steps"])
        result.update(state="queued" if queued else "working",
                      activity=f"{task.title} is queued for its next step." if queued else f"Working on {task.title}.",
                      continues_when_closed=server_only)
        return "Review the saved plan and progress. Deadlines are estimates."
    if task.state == "submitted" and task.kind == "one_time" and task.last_run_at:
        result.update(state="queued", activity=f"{task.title} is queued for its next step.", continues_when_closed=True)
        return "The task will continue from its saved checkpoint."
    if task.state == "submitted":
        activity = (f"{task.title} is waiting for its next scheduled run." if task.kind == "scheduled"
                    else f"{task.title} is waiting for its trigger event." if task.kind == "event"
                    else f"The plan for {task.title} is saved.")
        result.update(state="idle", activity=activity, continues_when_closed=task.kind != "one_time")
        return "Review the task's saved schedule and progress."
    result.update(state={"canceled": "cancelled", "rejected": "failed"}.get(task.state, task.state),
                  activity=(view["blocker"] or {}).get("reason") or {
                      "paused": f"You paused {task.title}. Saved progress remains available.",
                      "completed": f"Finished {task.title}. Its verified results are saved.",
                      "canceled": f"You stopped {task.title}. Saved results remain available.",
                  }.get(task.state, "The task stopped. Review its saved progress before continuing."),
                  continues_when_closed=False)
    return (view["action"]["label"] if task.state == "completed" and view["action"]["href"].startswith("/gtm?") else
        "Resume this task from the saved task list when ready." if task.state == "paused" else "Review the saved result before choosing what to do next.")


def _superseded_research_task(task, report):
    if (task.kind != "one_time" or task.state not in {"input_required", "failed"}
            or as_utc(task.updated_at) >= as_utc(report.completed_at or report.created_at)):
        return False
    try:
        reason = json.loads(task.blocker_json or "{}")
        specs = json.loads(task.scope_json or "{}").get("steps", [])
        return reason.get("class") in {"needs_input", "impossible"} and bool(specs) and all(
            spec.get("skill") == "research.deep" and spec.get("inputs", {}).get("revision_id") in {None, report.revision_id}
            for spec in specs)
    except (ValueError, TypeError, AttributeError):
        return False


def _newer_than_result(value, result: dict) -> bool:
    if value is None:
        return False
    current = result.get("changed_at")
    if not current:
        return True
    try:
        return as_utc(value) > as_utc(datetime.fromisoformat(current))
    except (TypeError, ValueError):
        return True


def _has_accepted_build(session, venture_id: str) -> bool:
    for run in session.scalars(select(BuildRun).where(BuildRun.venture_id == venture_id, BuildRun.runtime_version == 1)):
        try:
            spec = json.loads(run.spec or "{}")
            review = (spec.get("lifecycle") or {}).get("review") or {}
        except (TypeError, ValueError, AttributeError):
            continue
        if run.state == "succeeded" and run.closed_at is not None and spec.get("qa_state") == "passed" and review.get("status") == "accepted":
            return True
    return False


def _project_recent_task(result: dict, venture_id: str, task: PlannedTask | None,
                         *, public_site: dict | None = None, accepted_build: bool = True) -> None:
    if task is None or task.status not in {"open", "running", "done", "dismissed"}:
        return
    if getattr(task, "source", None) == "startup_priorities" and task.status == "open":
        return
    changed = task.finished_at or task.updated_at or task.created_at
    if not _newer_than_result(changed, result):
        return
    title = (task.title or "company work").strip()
    # "Next saved task... No work is in progress" must never replace a live job (e.g. Find customers).
    if (task.status == "open" and not public_site and result["state"] in {"working", "queued"}
            and (task.runnable or (not accepted_build and re.search(r"\bpublish", title, re.I)))):
        return
    href = f"/v/{venture_id}/tasks"
    result.update(
        stage="Company work",
        changed_at=as_utc(changed).isoformat(),
        job_id=None,
        continues_when_closed=task.status == "running",
        action={"label": "Open saved work", "href": href},
    )
    if task.status == "open":
        if public_site:
            result.update(state="needs_you", stage="Published site",
                          activity="A public landing page is live. Review the saved plan task against its remaining acceptance criteria.",
                          continues_when_closed=False)
        elif task.runnable or (not accepted_build and re.search(r"\bpublish", title, re.I)):
            # Publishing cannot be acted on before a build is accepted, so it is not "waiting" on the founder yet.
            result.update(state="idle", activity=f"Next saved task: {title}. No work is in progress.")
        else:
            result.update(state="needs_you", activity=f"{title} is waiting for a required action.", continues_when_closed=False)
    elif task.status == "running":
        result.update(state="working", activity=f"Working on {title}.")
    elif task.status == "dismissed":
        result.update(state="cancelled", activity=f"You dismissed {title}.", continues_when_closed=False)
    else:
        result.update(state="completed", activity=f"Finished {title}.", continues_when_closed=False)


def _project_recent_mac_task(result: dict, venture_id: str, order: VentureDocument | None) -> None:
    if order is None:
        return
    try:
        body = json.loads(order.provenance_json or "{}")
    except (TypeError, ValueError):
        return
    if not isinstance(body, dict):
        return
    pending_reconciliation = pending_mac_reconciliation(body)
    # A remaining reconciliation is still actionable even if a newer plan
    # snapshot was saved. Recency only orders terminal Mac history.
    if not pending_reconciliation and not _newer_than_result(order.updated_at, result):
        return
    state = str(body.get("state") or "")
    if body.get("reviewed_at") and state in {"claimed", "cancelled"}:
        state = "stopped"  # founder attested the Mac closed it; see device_orders.transition("acknowledge")
    if state not in {"pending", "approved", "claimed", "waiting_for_device", "stopped", "cancelled"}:
        return
    # An unclaimed review past its expiry can no longer be approved or started.
    if (state in {"pending", "approved"} and not body.get("claimed_at")
            and body.get("expires_at") and str(body["expires_at"]) <= utcnow().isoformat()):
        return
    native_stop = body.get("native_stop") if isinstance(body.get("native_stop"), dict) else {}
    clean_stop = bool(body.get("claimed_at") and body.get("closed_at")
                      and native_stop.get("processes_stopped") is True)
    verified = bool(clean_stop and native_stop.get("task_verified") is True
                    and native_stop.get("verification_kind")
                    and not body.get("cancelled_at")
                    and not body.get("stop_reconciliation_required")
                    and not body.get("navigation_effect_reconciliation")
                    and not body.get("startup_reconciliation"))
    # A settled cancellation or verified result is history, not a new blocker.
    # Keep a currently actionable stage (including a runnable saved task) in
    # front of it. Pending approval, running Mac work and unsettled stops still
    # take precedence, as does a cancelled order needing reconciliation.
    historical = ((state == "cancelled" and not pending_reconciliation)
                  or (state == "stopped" and (verified or body.get("reviewed_at"))))
    # A verified receipt can be the completion of an older queued background
    # step, so do not leave that queue projected as still running merely because
    # its job row has not yet caught up. Settled cancellations cannot complete
    # work and therefore never replace an active queue.
    if historical and (result.get("action") or result["state"] in {"needs_you", "failed"}
                       or (state == "cancelled" and result["state"] in {"queued", "working"})):
        return
    href = f"/v/{venture_id}/device-work-orders/{order.id}"
    action = {"label": "Review Mac task", "href": href}
    common = {
        "stage": "Computer use",
        "changed_at": as_utc(order.updated_at).isoformat(),
        "job_id": None,
        "action": action,
        "continues_when_closed": False,
    }
    if pending_reconciliation:
        result.update(**common, state="needs_you", activity=pending_reconciliation)
    elif state == "pending":
        result.update(**common, state="needs_you", activity="A Mac task is ready for permission review.")
    elif state == "approved":
        action["label"] = "Start reviewed Mac task"
        result.update(**common, state="needs_you", activity="The reviewed Mac task is approved and ready to start.")
    elif state == "claimed":
        result.update(**common, state="working", activity="Running the reviewed Mac task on this Mac.")
    elif state == "waiting_for_device":
        from app.build.device_orders import WAITING_FOR_DEVICE_MESSAGE
        result.update(**common, state="waiting_for_device", activity=WAITING_FOR_DEVICE_MESSAGE)
    elif state == "cancelled":
        result.update(**common, state="cancelled", activity="The reviewed Mac task was cancelled.")
    else:
        # A process-stop receipt settles execution; it says nothing about whether
        # the founder's goal was achieved. Older native clients do not report a
        # task outcome to Core, so a stopped order must remain reviewable.
        if verified or body.get("reviewed_at"):
            action["label"] = "Open Mac result"
            result.update(**common, state="completed",
                          activity="The reviewed Mac task was independently verified." if verified
                          else "You reviewed the stopped Mac task's outcome.")
            return
        activity = ("Mac task stopped, review the result" if clean_stop else
                    "The Mac task stopped, but its final receipt needs review.")
        from app.pagebuilder.local import saved_page_check_message
        activity = saved_page_check_message(body) or activity
        result.update(**common, state="needs_you", activity=activity)


def snapshot(venture_id: str, workspace_id: str | None, *, paused: bool = False) -> dict:
    base = f"/v/{venture_id}"
    result = {"state": "idle", "stage": "Saved venture", "activity": "No active background job recorded.",
              "changed_at": None, "job_id": None, "continues_when_closed": None,
              "progress": None, "action": None, "background_work": [], "deadline_at": None,
              "deadline_is_estimate": True, "research_report_ready": False}
    if not workspace_id:
        return result
    runtime_next = None
    handoff_next = None
    with db()() as session:
        from app.tasks.service import reverify_gtm_tasks
        if reverify_gtm_tasks(session, workspace_id=workspace_id, venture_id=venture_id):
            session.commit()
        scope = (Job.venture_id == venture_id, Job.workspace_id == workspace_id)
        projected_jobs = []
        for active in session.scalars(select(Job).where(*scope, Job.status.in_(ACTIVE))
                                      .order_by(Job.created_at.desc(), Job.id.desc())).yield_per(20):
            card = _background_card(session, active, venture_id)
            if card is not None:
                projected_jobs.append((active, card))
                if len(projected_jobs) == 5:
                    break
        active_jobs = [active for active, _ in projected_jobs]
        from app.research.progress import job_progress
        research_job = session.scalar(select(Job).where(*scope, Job.status.in_(ACTIVE),
            Job.kind.in_(("idea.validate", "research.deep", "gtm.journey.prepare"))).order_by(Job.created_at.desc(), Job.id.desc()).limit(1))
        if research_job and research_job.kind == "gtm.journey.prepare":
            from app.gtm.progress import compute_progress
            document = session.scalar(select(VentureDocument).where(VentureDocument.venture_id == venture_id,
                VentureDocument.kind == "gtm_journey"))
            data = json.loads(document.provenance_json or "{}") if document else {}
            result["research_progress"] = compute_progress(data, research_job) if data.get("job_id") == research_job.id else None
        else:
            result["research_progress"] = job_progress(session, research_job) if research_job else None
        result["background_work"] = [card for _, card in projected_jobs]
        job = active_jobs[0] if active_jobs else None
        if job is None:
            job = session.scalars(select(Job).where(*scope, Job.status.not_in(ACTIVE))
                                  .order_by(Job.created_at.desc(), Job.id.desc()).limit(1)).first()
        planned_candidates = session.scalars(select(PlannedTask).where(
            PlannedTask.venture_id == venture_id,
        ).order_by(PlannedTask.updated_at.desc(), PlannedTask.id.desc())).yield_per(100)
        planned = next(current_plan_rows(session, planned_candidates), None)
        public_site = (landing_publication_for_task(session, planned, workspace_id=workspace_id)
                       if planned and planned.status == "open" else None)
        accepted_build = _has_accepted_build(session, venture_id)
        mac_order = session.scalars(select(VentureDocument).where(
            VentureDocument.venture_id == venture_id,
            VentureDocument.kind == "device_work_order_v1",
        ).order_by(VentureDocument.updated_at.desc(), VentureDocument.id.desc()).limit(1)).first()
        # A later finished order must not bury an older unsettled hold. Narrow
        # the scan by the durable reconciliation key before parsing JSON.
        candidates = session.scalars(select(VentureDocument).where(
            VentureDocument.venture_id == venture_id,
            VentureDocument.kind == "device_work_order_v1",
            VentureDocument.provenance_json.contains('"stop_reconciliation_required"'),
        ).order_by(VentureDocument.updated_at.desc(), VentureDocument.id.desc())).all()
        for candidate in candidates:
            try:
                body = json.loads(candidate.provenance_json or "{}")
            except (TypeError, ValueError):
                continue
            if isinstance(body, dict) and pending_mac_reconciliation(body):
                mac_order = candidate
                break
        idea = session.scalars(select(IdeaSession).where(
            IdeaSession.venture_id == venture_id, IdeaSession.workspace_id == workspace_id,
            IdeaSession.state != "abandoned").order_by(IdeaSession.updated_at.desc()).limit(1)).first()
        report = current_research_report(session, idea)
        result["research_report_ready"] = report is not None
        if job:
            label, activity, route = WORK.get(job.kind, ("Background work", "Processing saved company work", "now"))
            changed = job.finished_at or job.started_at or job.created_at
            result.update(stage=label, changed_at=as_utc(changed).isoformat(), job_id=job.id)
            if job.status in ACTIVE:
                queued = job.status in {"scheduled", "pending", "retry_wait"}
                result.update(state="queued" if queued else "working",
                              activity="Retrying in a moment" if job.status == "retry_wait" else "Waiting for a worker" if queued else activity,
                              continues_when_closed=True if job.kind in WORK else None)
            elif job.status in FAILED:
                from app.chat.blockers import error_copy

                reason = error_copy(job.last_error)  # the stored code, in plain words
                if job.kind == "experiment.exploration.prepare" and str(job.last_error or "").partition(":")[0] == "handler_failed":
                    reason = "Something went wrong on our side while preparing your customer ideas. Your idea is saved. Open chat to retry, or tell me who you want to reach next."
                if job.kind == "research.deep" and job.last_error in {"research_outcome_uncertain", "job_outcome_uncertain", "research_interrupted"}:
                    reason = ("Research was interrupted. Your saved research report is still available. Open Research to review it or choose Run research again."
                              if report else "Research was interrupted. No usable research report was saved. Open Research and choose Run research again, or tell me to skip research for now.")
                action = _failed_action(job.kind, base)
                if str(job.last_error or "").partition(":")[0] in {
                    "clarification_insufficient_credits", "research_insufficient_credits", "plan_insufficient_credits",
                    "validation_insufficient_credits", "content_insufficient_credits", "gtm_insufficient_credits",
                    "pagebuilder_insufficient_credits",
                }:
                    from app.credits.recovery import eligible_job
                    if eligible_job(session, job):
                        reason = error_copy(job.last_error, credits_restored=True)
                        action = {"label": "Continue", "href": f"{base}/idea#journey-action"}
                    else:
                        action = {"label": "Request credits", "href": f"{base}/settings"}
                result.update(state="failed", action=action,
                              activity=reason or "This step needs attention. Your saved work is retained.")
                if job.kind == "research.deep":
                    handoff = _job_handoff(session, job, venture_id)
                    if handoff and handoff[0].state == "canceled":
                        result.update(state="cancelled", activity="Research skipped for now. " + (
                            "Your saved research report is still available. Open Research to review it." if report else
                            "No usable research report was saved. Open Research and choose Run research again when you want to continue."))
                if action["label"] == "Continue":
                    result["state"] = "needs_you"
            elif job.status == "cancelled":
                result.update(state="cancelled", activity="The last background job was cancelled.")
            else:
                result.update(state="completed", activity="The last background job completed.")
        # A successful worker can save incomplete research. Project the durable
        # result, not just transport/job success, and never read another tenant.
        research_review_action = {"label": "Review research", "href": f"{base}/research"}
        research_retry_action = {"label": "Run research again", "href": f"{base}/research"}
        if job and job.kind == "research.deep" and job.status in {"succeeded", "partial"}:
            dossier = session.scalars(select(ResearchDossier).where(
                ResearchDossier.id == research_identifier(job.id),
                ResearchDossier.workspace_id == workspace_id,
                ResearchDossier.venture_id == venture_id,
                ResearchDossier.kind == "deep")).first()
            if dossier and dossier.state == "failed":
                result.update(state="needs_you", stage="Research",
                              activity="Research did not finish. Your idea is saved. Open Research and choose Run research again.",
                              action=research_retry_action)
            elif not report:
                result.update(state="needs_you", stage="Research",
                              activity="No usable research report was saved. Open Research and choose Run research again.",
                              action=research_retry_action)
            elif dossier and dossier.state == "partial":
                result.update(state="needs_you", stage="Research",
                              activity="Research is ready. Review it before planning.",
                              action=research_review_action)
        from app.pagebuilder.api import enabled as page_builder_enabled

        # With the one-click page builder the plan is optional: a ready plan never demands review.
        if job and job.kind == "plan.compile" and job.status == "succeeded" and not page_builder_enabled():
            plan = session.scalars(select(ExecutionPlan).where(
                ExecutionPlan.id == plan_identifier(job.id),
                ExecutionPlan.workspace_id == workspace_id,
                ExecutionPlan.venture_id == venture_id)).first()
            if plan and plan.state == "founder_review":
                result.update(state="needs_you", stage="Planning",
                              activity="Your execution plan is ready for review.",
                              action={"label": "Review execution plan", "href": f"{base}/plan#plan-review"})
        # Recurring monitors are queued between checks while the build runs.
        # Project the durable build, not that scheduling state or job success.
        if job and job.kind in {"build.run", "build.watch", "qa.run"} and job.status in (*ACTIVE, *FAILED, "cancelled", "succeeded"):
            try:
                payload = json.loads(job.payload_json or "{}")
                run_id = payload.get("build_run_id") if isinstance(payload, dict) else None
            except (TypeError, ValueError):
                run_id = None
            build = session.scalar(select(BuildRun).where(BuildRun.id == run_id,
                BuildRun.workspace_id == workspace_id, BuildRun.venture_id == venture_id,
                BuildRun.runtime_version == 1)) if run_id else None
            if build:
                try:
                    spec = json.loads(build.spec or "{}")
                    if not isinstance(spec, dict):
                        spec = {}
                except (TypeError, ValueError):
                    spec = {}
                landing = landing_build_result(session, build, spec)
                build_href = f"{base}/{'site' if landing is not None else 'builds'}?" + urlencode({"app": build.app_id, "run": build.id}) + ("#build-review" if landing is not None else "")
                changed = build.finished_at or build.started_at or build.created_at
                if changed:
                    result["changed_at"] = as_utc(changed).isoformat()
                result["continues_when_closed"] = not bool(build.closed_at)
                if landing and landing["available"]:
                    result.update(state="ready", stage="Landing page", landing_page=landing,
                        activity="Your landing page is ready." + (" " + landing["qa_note"] if landing["qa_note"] else ""),
                        action={"label": "Visit landing page" if landing["live_url"] else "Review page",
                                "href": landing["live_url"] or build_href})
                    if landing["live_url"] and landing["qa_note"]:
                        result["secondary_action"] = {"label": "Review QA notes" if landing.get("qa_report_available") else "Review saved attempt", "href": build_href}
                elif build.state in {"failed", "cancelled", "rejected"}:
                    activity = ("The build reached its reviewed time limit before source and QA were verified."
                                if build.error_code == "build_runtime_deadline" else
                                "The build did not complete successfully. Review its saved result and usage.")
                    result.update(state="cancelled" if build.state == "cancelled" else "failed",
                        activity=activity,
                        action={"label": "Try again", "href": f"{base}/site?build=1"} if landing is not None
                            else {"label": "Review build result", "href": build_href})
                    if landing is not None:
                        result["secondary_action"] = {"label": "Review saved result", "href": build_href}
                elif build.state == "succeeded":
                    try:
                        spec = json.loads(build.spec or "{}")
                        review_status = spec.get("lifecycle", {}).get("review", {}).get("status")
                        accepted = bool(spec.get("execution_verified") and review_status == "accepted")
                    except (TypeError, ValueError, AttributeError):
                        review_status = None
                        accepted = False
                    if review_status == "rejected":
                        result.update(state="needs_you",
                            activity="Changes requested. Review the saved feedback and build evidence before planning a repair.",
                            action={"label": "Review requested changes", "href": build_href})
                    else:
                        result.update(state="completed" if accepted else "needs_you",
                            activity="The verified build was accepted." if accepted else "Build work finished. Review the source, QA and acceptance evidence.",
                            action={"label": "Review build evidence", "href": build_href + "#build-review"})
                else:
                    try:
                        spec = json.loads(build.spec or "{}")
                        phase = spec.get("phase") if isinstance(spec, dict) else None
                    except (TypeError, ValueError):
                        phase = None
                    stage, activity = {
                        "provisioning_sandbox": ("Building", "Preparing the isolated build environment."),
                        "running": ("Building", "Building the approved product."),
                        "artifact_pending": ("Build verification", "Capturing the generated source for independent QA."),
                        "builder_stopping": ("Build verification", "Stopping the builder before independent QA."),
                        "qa_pending": ("Testing", "Preparing the captured source for independent QA."),
                        "qa_ready": ("Testing", "Starting independent browser QA."),
                        "qa_running": ("Testing", "Independent browser QA is checking the built product."),
                        "qa_stopping": ("Build verification", "Stopping the QA runtime and checking its receipts."),
                        "usage_reconciliation_required": ("Build verification", "Reconciling usage before releasing unused credits."),
                    }.get(phase or build.state, ("Build verification", "Build work or cleanup is still in progress."))
                    result.update(state="working", stage=stage, activity=activity,
                        action={"label": "View build progress", "href": build_href})
            elif job.status == "succeeded":
                result.update(state="needs_you", activity="The monitor ended, but its build result could not be verified.",
                    action={"label": "Check build evidence", "href": f"{base}/builds"})
        runtime = session.scalar(select(AppRuntimeIntent).where(
            AppRuntimeIntent.workspace_id == workspace_id,
            AppRuntimeIntent.venture_id == venture_id,
        ).order_by(AppRuntimeIntent.created_at.desc(), AppRuntimeIntent.id.desc()).limit(1))
        # An active, different build is more immediate than an older app's
        # runtime review. Otherwise the durable runtime phase supersedes the
        # source-build job, whose success did not establish a usable product.
        newer_other_build = bool(job and job.status in ACTIVE and job.kind in {
            "build.run", "build.watch", "qa.run"
        } and runtime and job.created_at > runtime.created_at)
        if runtime and runtime.state in {"pending", "provisioning", "reconciling", "staged", "review", "failed"} and not newer_other_build:
            href = f"{base}/builds?" + urlencode({"app": runtime.app_id, "run": runtime.build_run_id})
            phase_activity = {
                "source_accepted": "Preparing the approved application source for deployment.",
                "database_secret_created": "Preparing the application's private database.",
                "database_provisioning": "Provisioning the application's private database.",
                "auth_provisioned": "Preparing customer sign-in for the application.",
                "image_building": "Building the isolated application image.",
                "image_ready": "Preparing the application's database migration.",
                "runtime_migrated": "Starting the application service.",
                "service_staged": "Checking the application service and domain.",
                "preview_live": "Waiting for the application's domain to become ready.",
            }
            if runtime.state == "review":
                result.update(state="needs_you", stage="Application QA",
                    activity="The private application is ready for functional review; sign-in, saved data, exports and account isolation are unverified.",
                    action={"label": "Review application runtime", "href": href},
                    continues_when_closed=False)
                runtime_next = "Test the private application before inviting users."
            elif runtime.state == "failed":
                result.update(state="failed", stage="Application setup",
                    activity="Application provisioning stopped. Review the saved result before retrying.",
                    action={"label": "Review application setup", "href": href},
                    continues_when_closed=False)
            else:
                result.update(state="working", stage="Application setup",
                    activity=phase_activity.get(runtime.phase, "Preparing the application runtime."),
                    action={"label": "View application setup", "href": href},
                    continues_when_closed=True)
                runtime_next = "Wait for the private application preview, then test its functional workflow."
            result["changed_at"] = as_utc(runtime.updated_at).isoformat()
        if idea:
            personal = False
            if idea.state in {"reviewing", "confirmed"}:
                from app.idea.service import _project_intent
                revision = session.get(IdeaRevision, idea.current_revision_id) if idea.current_revision_id else None
                try:
                    structured = json.loads(revision.structured or "{}") if revision else {}
                except (TypeError, ValueError):
                    structured = {}
                personal = (_project_intent(session, workspace_id, venture_id) == "personal"
                            and isinstance(structured, dict) and structured.get("project_intent") == "personal")
            # Research is outside a confirmed personal project's current route.
            # A legacy worker may save its terminal dossier after confirmation,
            # so job timestamps cannot make its evidence gaps the next action.
            # Yield only when that terminal review is still the projection;
            # live work, failures and runtime attention continue to win.
            old_research_review = (personal and idea.state == "confirmed"
                                   and job is not None and job.kind == "research.deep"
                                   and job.status == "succeeded"
                                   and result["state"] == "needs_you"
                                   and result["action"] in (research_review_action, research_retry_action))
            old_personal_validation = (personal and idea.state == "confirmed"
                                       and job is not None and job.kind == "idea.validate")
            if (result["state"] not in {"needs_you", "failed", "cancelled", "working"}
                    or old_research_review or old_personal_validation) and not (
                    job and job.status in (*ACTIVE, *FAILED) and not old_personal_validation):
                turn = session.scalars(select(ClarifierTurn).where(ClarifierTurn.session_id == idea.id)
                                       .order_by(ClarifierTurn.turn.desc()).limit(1)).first()
                if turn and turn.role != "founder" and turn.kind == "question":
                    result.update(state="needs_you", stage="Clarification", activity="Your next answer is needed.",
                                  changed_at=as_utc(turn.created_at).isoformat(),
                                  action={"label": "Answer question", "href": f"{base}/idea#journey-action"})
                elif idea.state == "reviewing" or (idea.state == "validating" and validation_settled(session, idea)):
                    if personal:
                        result.update(state="needs_you", stage="Project review",
                                      activity="Your personal project is ready for review before a local build.",
                                      action={"label": "Review project", "href": f"{base}/idea#journey-action"})
                    elif validation_settled(session, idea, include_accepted=True):
                        result.update(state="needs_you", activity="Review the saved validation before continuing.",
                                      action={"label": "Review validation", "href": f"{base}/idea#journey-action"})
                    else:
                        result.update(state="needs_you", stage="Idea review", activity="Review your saved idea before validation.",
                                      action={"label": "Review idea", "href": f"{base}/idea#journey-action"})
                elif idea.state == "confirmed" and personal:
                    result.update(state="needs_you", stage="Local build",
                                  activity="Your confirmed personal project is ready for a reviewed local build.",
                                  changed_at=as_utc(idea.confirmed_at or idea.updated_at).isoformat(),
                                  job_id=None, continues_when_closed=False,
                                  action={"label": "Review local build", "href": f"{base}/idea#journey-action"})
        # An unclaimed slate item cannot supersede a live durable job merely
        # because the plan snapshot was saved later than the job began.
        if not (job and job.status in ACTIVE and planned and planned.status == "open"):
            _project_recent_task(result, venture_id, planned, public_site=public_site, accepted_build=accepted_build)
        # A rejected, closed build remains an unresolved product review even
        # after a later chat task changes the slate or the monitor job ages out.
        # Use the latest venture-scoped build receipt, never a task title.
        latest_build = session.scalar(select(BuildRun).where(
            BuildRun.workspace_id == workspace_id,
            BuildRun.venture_id == venture_id,
            BuildRun.runtime_version == 1,
        ).order_by(BuildRun.created_at.desc(), BuildRun.id.desc()).limit(1))
        if latest_build and result["state"] not in {"queued", "working", "paused"} and (
                job is None or job.kind in {"build.run", "build.watch", "qa.run", "pagebuilder.build", "deploy.run"}):
            try:
                latest_spec = json.loads(latest_build.spec or "{}")
                landing = landing_build_result(session, latest_build, latest_spec)
            except (TypeError, ValueError, AttributeError):
                landing = None
            if landing and landing["available"]:
                result.update(state="ready", stage="Landing page", landing_page=landing,
                    activity="Your landing page is ready." + (" " + landing["qa_note"] if landing["qa_note"] else ""),
                    action={"label": "Visit landing page" if landing["live_url"] else "Review page",
                            "href": landing["live_url"] or f"{base}/site?" + urlencode({"app": latest_build.app_id, "run": latest_build.id}) + "#build-review"})
                if landing["live_url"] and landing["qa_note"]:
                    result["secondary_action"] = {"label": "Review QA notes" if landing.get("qa_report_available") else "Review saved attempt",
                        "href": f"{base}/site?" + urlencode({"app": latest_build.app_id, "run": latest_build.id}) + "#build-review"}
            elif landing is not None and latest_build.state in {"failed", "cancelled", "rejected"}:
                from app.tasks.contracts import blocker
                problem = blocker(latest_build.error_code or "pagebuilder_qa_failed")
                result.update(state="cancelled" if latest_build.state == "cancelled" else "failed", stage="Landing page",
                    activity=problem["reason"] + " " + problem["unblock_action"],
                    action={"label": "Try again", "href": f"{base}/site?build=1"},
                    secondary_action={"label": "Review saved result", "href": f"{base}/site?" + urlencode({"app": latest_build.app_id, "run": latest_build.id}) + "#build-review"})
        if latest_build and latest_build.state == "succeeded" and latest_build.closed_at:
            try:
                latest_spec = json.loads(latest_build.spec or "{}")
                review = latest_spec.get("lifecycle", {}).get("review", {})
            except (TypeError, ValueError, AttributeError):
                review = {}
            if isinstance(review, dict) and review.get("status") == "rejected" and result["state"] not in {"queued", "working", "paused", "ready"}:
                review_href = f"{base}/{'site' if landing_build_result(session, latest_build, latest_spec) is not None else 'builds'}?"
                result.update(state="needs_you", stage="Build review",
                              activity="Changes requested. Review the saved feedback and build evidence before planning a repair.",
                              changed_at=as_utc(latest_build.closed_at).isoformat(),
                              job_id=None, continues_when_closed=False,
                              action={"label": "Review requested changes", "href": review_href + urlencode({"app": latest_build.app_id, "run": latest_build.id})})
        _project_recent_mac_task(result, venture_id, mac_order)
        if (report and not active_jobs and not personal
                and (not job or job.kind in {"research.deep", "idea.validate", "tasks.execute"})
                and result["stage"] in {"Saved venture", "Research", "Validation", "Your task"}
                and _newer_than_result(report.completed_at or report.created_at, result)):
            result.update(state="completed", stage="Research", job_id=None, continues_when_closed=False,
                          changed_at=as_utc(report.completed_at or report.created_at).isoformat(),
                          activity="Research is ready. Review it before planning." if report.state == "partial"
                          else "Your research report is saved. Open Research to review it, then choose your next priority.",
                          action={"label": "Review research", "href": f"{base}/research"})
        handoff_next = _project_handoffs(session, result, venture_id, workspace_id, report)
        active_page = next(((row, card) for row, card in projected_jobs if row.kind == "pagebuilder.build"), None)
        if active_page and (result["stage"] == "Landing page" or result.get("job_id") == active_page[0].id):
            row, card = active_page
            result.pop("landing_page", None)
            result.pop("secondary_action", None)
            result.update(state=card["state"], stage=card["stage"], activity=card["activity"],
                changed_at=card["changed_at"], job_id=row.id, continues_when_closed=True,
                action={"label": "View build progress", "href": card["href"]})
            handoff_next = runtime_next = None
        if result["state"] not in {"queued", "working", "paused"}:
            # ponytail: same 20-job history as Website, index execution time if old requeues outgrow it.
            candidates = session.scalars(select(Job).where(*scope, Job.kind == "pagebuilder.build")
                .order_by(func.coalesce(Job.scheduled_for, Job.created_at).desc(), Job.created_at.desc()).limit(20)).all()
            matching = []
            for row in candidates:
                try:
                    if not idea or json.loads(row.payload_json).get("revision_id") == idea.confirmed_revision_id:
                        matching.append(row)
                except (TypeError, ValueError, AttributeError):
                    continue
            candidate = max(matching, default=None, key=lambda row: max(as_utc(value) for value in
                (row.created_at, row.scheduled_for, row.started_at, row.finished_at) if value))
            if candidate:
                from app.pagebuilder.api import _state
                try:
                    candidate_doc = json.loads(candidate.payload_json)
                    candidate_state, candidate_code = _state(candidate, candidate_doc)
                except (KeyError, TypeError, ValueError):
                    candidate_state, candidate_code = None, None
                activity = max(as_utc(value) for value in (candidate.created_at, candidate.scheduled_for, candidate.started_at, candidate.finished_at) if value)
                if candidate_state == "failed" and (result.get("job_id") == candidate.id or _newer_than_result(activity, result)):
                    from app.chat.blockers import error_copy
                    result.pop("landing_page", None)
                    result.pop("secondary_action", None)
                    result.update(state="failed", stage="Landing page", activity=error_copy(candidate_code),
                        job_id=candidate.id, changed_at=as_utc(activity).isoformat(),
                        continues_when_closed=False, action={"label": "Build my page again", "href": f"{base}/site?build=1"})
                    handoff_next = runtime_next = None
        from app.ventures.startup_review import enabled, review, route
        venture = session.get(Venture, venture_id)
        if (venture and venture.workspace_id == workspace_id and enabled(session, venture_id)
                and idea and (idea.state == "confirmed" or validation_settled(session, idea, include_accepted=True))
                and not personal and not active_jobs and not handoff_next and not runtime_next
                and (result["action"] is None or result["action"]["href"].startswith(base + "/idea"))
                and result["state"] not in {"failed", "cancelled", "working", "waiting_for_device"}):
            context = review(session, venture)
            if not context["complete"] or not context["choices"].get("target"):
                destination = route(venture_id, context["priorities"]) if context["complete"] else {
                    "title": "Review your startup and priorities", "href": base + "/context"}
                result.update(state="needs_you", stage="Your next step", activity=destination["title"],
                              job_id=None, continues_when_closed=False,
                              action={"label": destination["title"], "href": destination["href"]})
        if paused:
            result.update(state="paused", activity="You paused this venture.", action=None,
                          continues_when_closed=False)
    result["next_step"] = next_step(result, job.kind if job else None)
    if handoff_next and not paused:
        result["next_step"] = handoff_next
    elif runtime_next and not paused:
        result["next_step"] = runtime_next
    return result


def next_step(result: dict, kind: str | None) -> str:
    """Describe the workflow's next checkpoint, never schedule or authorize it."""
    state = result["state"]
    if state in {"ready", "completed"} and kind == "gtm.journey.prepare":
        return (result.get("action") or {}).get("label", "Find customers")
    if state == "ready" and result.get("landing_page"):
        return ("Visit your landing page, copy its link, or continue to Find customers." if result["landing_page"].get("live_url") else
                "Review the saved page and its latest checks before publishing or building again.")
    if state == "paused":
        return "Review the saved work and resume the venture when ready."
    if state in {"failed", "cancelled"}:
        return "Review the saved result before deciding whether to retry or change the plan."
    if state == "needs_you":
        return (result.get("action") or {}).get("label", "Review the required action.")
    if state in {"queued", "working"}:
        return {
            "gtm.god.run": ("Go-to-market diagnosis", "Preparing your go-to-market diagnosis", "now"),
    "content.draft": ("Content draft", "Writing a content draft", "now"),
    "content.topics": ("Content ideas", "Finding content ideas", "now"),
    "task.run": ("Company task", "Running a saved company task", "tasks"),
    "idea.clarify_turn": "Answer the next clarification question when it is saved.",
            "idea.validate": "Review the validation and its evidence gaps.",
            "research.deep": "Review the saved research before planning.",
            "plan.compile": "Review the generated plan before approving execution.",
            "build.run": "Independent QA, usage settlement, then your build review.",
            "build.watch": "Wait for verified build and QA receipts, then review the result.",
            "qa.run": "Review the QA result before accepting the build.",
            "deploy.run": "Verify the live URL and conversion path after publication.",
        }.get(kind, "Review the saved result when this work finishes.")
    return "Open the venture's saved work to review the next available action."
