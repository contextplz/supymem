"""Planned tasks API."""

from __future__ import annotations

import json
from types import SimpleNamespace

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select

from app.db.models import PlannedTask, Venture
from app.db.supymem_models import BuildRun, ExecutionPlan, PlanMilestone, PlanTask
from app.planning.execute import TaskRunError, start_planned_task
from app.planning.store import current_plan_work, list_open_tasks, list_tasks as list_tasks_by_status
from app.planning.publication_evidence import public_site_evidence
from app.state import db
from core.clock import as_utc, utcnow
from core.ids import new_id

router = APIRouter(tags=["tasks"])


class DismissBody(BaseModel):
    reason: str = "not_relevant"


class SummaryBody(BaseModel):
    title: str | None = None
    refresh: bool = False


def _plan_and_save(venture_id: str, *, source: str) -> dict:
    factory = db()
    with factory() as session:
        ven = session.get(Venture, venture_id)
        if not ven:
            raise HTTPException(404, "not_found")
        from app.ventures.current_idea import current_idea
        idea = current_idea(ven, session=session)
        name = ven.name
    from app.onboarding.state import Ctx, latest_run
    from app.onboarding.narrator import Narrator
    from app.planning.generate import generate_next_tasks
    from app.planning.store import save_tasks

    run = latest_run(venture_id)
    from app.ventures.journey_overrides import NOTE, read as journey_choices
    skipped = journey_choices(venture_id)["skipped"]
    if run and not skipped:
        ctx = Ctx.build(run, narrator=Narrator(venture_id, run.id))
    else:
        ctx = SimpleNamespace(
            venture=SimpleNamespace(id=venture_id, raw_idea=idea, name=name),
            company=name or "this venture",
        )
    plan = generate_next_tasks(ctx)
    saved = save_tasks(venture_id, plan["tasks"], source=source)
    return {"headline": plan.get("headline"), "tasks": saved, "id": new_id("rpl"),
            "note": NOTE if skipped else "Suggested tasks use the available context; missing evidence stays unknown."}


def _refresh_approved_plan_work(venture_id: str) -> dict | None:
    """Keep a founder-approved plan authoritative over the generic discovery slate."""
    from app.planning.compiler import _approved_work
    from app.planning.store import publish_saved_tasks, save_tasks

    with db()() as session:
        plan = session.scalar(select(ExecutionPlan).where(
            ExecutionPlan.venture_id == venture_id,
            ExecutionPlan.state.in_(("approved", "executing")),
            ExecutionPlan.superseded_by.is_(None),
        ).order_by(ExecutionPlan.version.desc()).limit(1))
        if plan is None:
            return None
        plan_id = plan.id
        doc = json.loads(plan.structured)
        task_ids = list(doc["entity_ids"]["tasks"].values())
        completed = frozenset(session.scalars(select(PlanTask.id).where(
            PlanTask.id.in_(task_ids), PlanTask.state == "done")).all())
        tasks = _approved_work(plan_id, doc, completed_task_ids=completed)
        saved = save_tasks(venture_id, tasks, source=f"approved_plan:{plan_id}",
                           session=session, publish=False)
        session.commit()
    publish_saved_tasks(venture_id, saved)
    return {"headline": "Work refreshed from the current founder-approved execution plan.",
            "tasks": saved, "id": new_id("rpl"), "plan_id": plan_id}


# Plan-task states where the build is no longer executing (awaiting review, or ended).
_BUILD_SETTLED = frozenset({"waiting_for_founder", "failed", "cancelled", "blocked"})


def _replan_available(facts: dict) -> bool:
    return (bool((facts.get("journey_overrides") or {}).get("skipped"))
            or facts.get("idea_state") is None or facts.get("plan_state") in {"approved", "executing"})


@router.get("/ventures/{venture_id}/tasks")
def list_venture_tasks(venture_id: str):
    factory = db()
    with factory() as session:
        if not session.get(Venture, venture_id):
            raise HTTPException(404, "not_found")
        venture = session.get(Venture, venture_id)
        items = list_open_tasks(venture_id)
        running = list_tasks_by_status(venture_id, statuses=("running",))
        # Approved-plan Work rows are a snapshot. The plan task is the
        # authority for completion and build review, including changes made
        # outside Work. Project that state without rewriting historical rows.
        bound = [task for task in items + running if task.get("source", "").startswith("approved_plan:")]
        task_ids = {
            tag.removeprefix("plan_task:")
            for task in bound for tag in task.get("tags", [])
            if tag.startswith("plan_task:")
        }
        plan_tasks = {
            row.id: row for row in session.scalars(select(PlanTask).where(
                PlanTask.id.in_(task_ids), PlanTask.venture_id == venture_id, PlanTask.workspace_id == venture.workspace_id,
            )).all()
        } if task_ids else {}
        milestones = {
            row.id: row for row in session.scalars(select(PlanMilestone).where(
                PlanMilestone.id.in_({row.milestone_id for row in plan_tasks.values()}),
                PlanMilestone.venture_id == venture_id, PlanMilestone.workspace_id == venture.workspace_id,
            )).all()
        } if plan_tasks else {}
        publication = public_site_evidence(session, workspace_id=venture.workspace_id,
                                           venture_id=venture_id) if any(
            row.kind == "landing" for row in milestones.values()) else None
        plan_scope_cache: dict[str, dict] = {}

        def current(tasks: list[dict]) -> list[dict]:
            result = []
            for task in tasks:
                saved = session.get(PlannedTask, task["id"])
                if saved is not None and not current_plan_work(session, saved):
                    continue
                if not task.get("source", "").startswith("approved_plan:"):
                    result.append(task)
                    continue
                plan_task_id = next((tag.removeprefix("plan_task:") for tag in task["tags"]
                                     if tag.startswith("plan_task:")), None)
                plan_task = plan_tasks.get(plan_task_id)
                milestone = milestones.get(plan_task.milestone_id) if plan_task else None
                if not milestone or milestone.plan_id != task["source"].removeprefix("approved_plan:"):
                    result.append(task)
                    continue
                if milestone.plan_id not in plan_scope_cache:
                    source_plan = session.get(ExecutionPlan, milestone.plan_id)
                    plan_scope_cache[milestone.plan_id] = (
                        json.loads(source_plan.structured).get("scope_details", {})
                        if source_plan and source_plan.venture_id == venture_id else {}
                    )
                scope = plan_scope_cache[milestone.plan_id].get(plan_task.id, {})
                task["plan_scope"] = scope.get("scope")
                task["plan_scope_reason"] = scope.get("reason") if scope.get("scope") == "out" else None
                if scope.get("scope") == "out":
                    task["runnable"] = False
                if plan_task.state == "done":
                    continue
                task["plan_task_state"] = plan_task.state
                task["due_at"] = as_utc(plan_task.due_at).isoformat() if plan_task.due_at else None
                task["deadline_is_estimate"] = True
                if task.get("status") == "running" and plan_task.state in _BUILD_SETTLED:
                    task["status"] = "open"  # the build run, not the saved row, says whether work is still running
                if publication and milestone.kind == "landing":
                    task["public_site_evidence"] = publication
                if "capability:build.run" in task["tags"]:
                    run = session.get(BuildRun, plan_task.build_run_id) if plan_task.build_run_id else None
                    task["build_review_ready"] = bool(
                        plan_task.state == "waiting_for_founder"
                        and run and run.venture_id == venture_id
                        and run.plan_task_id == plan_task.id
                        and run.state == "succeeded" and run.closed_at
                    )
                result.append(task)
            return result

        merged = current(items) + current(running)
        items = sorted((t for t in merged if t.get("status") != "running"), key=lambda t: t.get("rank") or 0)
        running = [t for t in merged if t.get("status") == "running"]
    from app.ventures.next_step import _facts, founder_next_step
    try:
        facts = _facts(venture_id)
    except KeyError:  # journey state unavailable: the task list itself must still load
        facts = {}
    empty_action = None
    if not items and not running and facts:  # the next-step resolver is only needed for the empty Work state
        step = founder_next_step(venture_id)
        empty_action = {"label": step["cta_label"], "href": step["href"]}
        if facts.get("plan_state") == "founder_review" and facts.get("plan_current"):
            empty_action = {"label": "Review your plan", "href": f"/v/{venture_id}/plan"}
    return {
        "items": items,
        "running": running,
        "next_cursor": None,
        "replan_available": _replan_available(facts),
        "empty_action": empty_action,
        "note": "No saved tasks yet; draft your own work or request suggestions from available context." if not items and not running else None,
    }


@router.post("/ventures/{venture_id}/tasks/plan")
def plan_tasks(venture_id: str):
    """Generate the next-five queue even if onboarding has not finished."""
    return _plan_and_save(venture_id, source="plan")


@router.post("/ventures/{venture_id}/tasks/{task_id}/summary")
async def summarize_task_view(venture_id: str, task_id: str, payload: SummaryBody | None = None):
    """Write a founder brief for one task. Generated on click; cached after."""
    payload = payload or SummaryBody()
    factory = db()
    with factory() as session:
        if not session.get(Venture, venture_id):
            raise HTTPException(404, "not_found")
    from app.planning.summarize import summarize_task

    brief = await summarize_task(
        venture_id,
        task_id=task_id,
        title=payload.title,
        refresh=payload.refresh,
    )
    if not brief:
        raise HTTPException(404, "not_found")
    return brief


@router.post("/ventures/{venture_id}/tasks/{task_id}/run")
def run_task(venture_id: str, task_id: str):
    """Queue a planned task run (202); the worker executes it and leaves an artifact + chat note.

    Named employee speaks for the work. Coding-shaped tasks try the dispatcher
    and always fall back to a local artifact — BYO CLI is never required.
    """
    try:
        return JSONResponse(start_planned_task(venture_id, task_id), status_code=202)
    except TaskRunError as exc:
        raise HTTPException(
            exc.status,
            detail={"error": {"code": exc.code, "message": exc.message}},
        ) from exc


@router.post("/ventures/{venture_id}/tasks/{task_id}/dismiss")
def dismiss_task(venture_id: str, task_id: str, payload: DismissBody | None = None):
    payload = payload or DismissBody()
    factory = db()
    with factory() as session:
        row = session.get(PlannedTask, task_id)
        if not row or row.venture_id != venture_id:
            raise HTTPException(404, "not_found")
        blocked_provider = None
        if (row.last_error or "").startswith("needs_you:"):
            blocked_provider = (row.last_error or "").split(":", 1)[1]
        row.status = "dismissed"
        row.updated_at = utcnow()
        row.finished_at = utcnow()
        row.last_error = payload.reason
        session.commit()
        result = {"id": row.id, "status": "dismissed"}
    if blocked_provider:
        from app.concierge.human import dismiss_provider_attention_if_unused

        dismiss_provider_attention_if_unused(venture_id, blocked_provider)
    return result


@router.post("/ventures/{venture_id}/tasks/replan")
def replan_tasks(venture_id: str):
    approved = _refresh_approved_plan_work(venture_id)
    if approved is not None:
        return approved
    from app.ventures.next_step import _facts

    f = _facts(venture_id)
    if not _replan_available(f):
        raise HTTPException(409, detail={"error": {
            "code": "replan_needs_approved_plan",
            "message": "Replan is available once your idea is confirmed and an execution plan is approved. "
                       "Clarification, validation, research and planning come first; your saved work is untouched."}})
    return _plan_and_save(venture_id, source="replan")
