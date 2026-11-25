"""Workflow CRUD and run lifecycle (40 §8)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import select

from app.audit.writer import audit
from app.authz.gate import Denied, check
from app.db.models import Workflow, WorkflowRun
from app.state import db
from app.workspaces.resolve import LOCAL_PERSON_ID
from app.workflows.shadow import is_shadow_run
from core.clock import utcnow
from core.ids import new_id

VALID_STATUSES = frozenset({"draft", "shadow", "active", "paused", "retired"})
VALID_SOURCES = frozenset({"described", "imported", "inferred", "template"})
MAX_STEPS = 12


class WorkflowError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class WorkflowView:
    id: str
    workspace_id: str
    name: str
    description: str
    trigger: dict[str, Any]
    steps: list[dict[str, Any]]
    owner_id: str
    autonomy: str
    status: str
    paused_reason: str | None
    source: str
    version: int
    cost_cap_run_usd: float
    cost_cap_month_usd: float
    created_at: str
    updated_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "name": self.name,
            "description": self.description,
            "trigger": self.trigger,
            "steps": self.steps,
            "owner_id": self.owner_id,
            "autonomy": self.autonomy,
            "status": self.status,
            "paused_reason": self.paused_reason,
            "source": self.source,
            "version": self.version,
            "cost_cap_run_usd": self.cost_cap_run_usd,
            "cost_cap_month_usd": self.cost_cap_month_usd,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "shadow_mode": self.status in {"shadow", "draft"},
        }


@dataclass(frozen=True)
class RunView:
    id: str
    workflow_id: str
    workflow_version: int
    engagement_id: str | None
    status: str
    shadow: bool
    idempotency_key: str
    simulated_writes: list[dict[str, Any]]
    outcome: dict[str, Any]
    cost_usd: float
    started_at: str
    finished_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workflow_id": self.workflow_id,
            "workflow_version": self.workflow_version,
            "engagement_id": self.engagement_id,
            "status": self.status,
            "shadow": self.shadow,
            "idempotency_key": self.idempotency_key,
            "simulated_writes": self.simulated_writes,
            "outcome": self.outcome,
            "cost_usd": self.cost_usd,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


def _json_obj(raw: str, default: Any) -> Any:
    try:
        return json.loads(raw or json.dumps(default))
    except json.JSONDecodeError:
        return default


def _workflow_view(row: Workflow) -> WorkflowView:
    return WorkflowView(
        id=row.id,
        workspace_id=row.workspace_id,
        name=row.name,
        description=row.description or "",
        trigger=_json_obj(row.trigger_json, {}),
        steps=_json_obj(row.steps_json, []),
        owner_id=row.owner_id,
        autonomy=row.autonomy,
        status=row.status,
        paused_reason=row.paused_reason,
        source=row.source,
        version=row.version,
        cost_cap_run_usd=float(row.cost_cap_run_usd or 0),
        cost_cap_month_usd=float(row.cost_cap_month_usd or 0),
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else "",
    )


def _run_view(row: WorkflowRun) -> RunView:
    return RunView(
        id=row.id,
        workflow_id=row.workflow_id,
        workflow_version=row.workflow_version,
        engagement_id=row.engagement_id,
        status=row.status,
        shadow=bool(row.shadow),
        idempotency_key=row.idempotency_key,
        simulated_writes=_json_obj(row.simulated_writes_json, []),
        outcome=_json_obj(row.outcome_json, {}),
        cost_usd=float(row.cost_usd or 0),
        started_at=row.started_at.isoformat() if row.started_at else "",
        finished_at=row.finished_at.isoformat() if row.finished_at else None,
    )


def _validate_steps(steps: list[dict[str, Any]]) -> None:
    if len(steps) > MAX_STEPS:
        raise WorkflowError(
            "too_many_steps",
            f"A workflow may have at most {MAX_STEPS} steps.",
        )
    allowed = {"connector_read", "connector_write", "agent", "rule", "human_task"}
    for i, step in enumerate(steps, start=1):
        action = step.get("action") or {}
        kind = action.get("kind")
        if kind not in allowed:
            raise WorkflowError(
                "invalid_step",
                f"Step {i} has unknown action kind: {kind!r}.",
            )


async def create_workflow(
    *,
    workspace_id: str,
    name: str,
    description: str = "",
    trigger: dict[str, Any] | None = None,
    steps: list[dict[str, Any]] | None = None,
    owner_id: str = LOCAL_PERSON_ID,
    source: str = "described",
    autonomy: str = "propose",
    shadow_mode: bool = True,
    actor_id: str = LOCAL_PERSON_ID,
    surface: str = "api",
) -> WorkflowView:
    decision = await check(
        "request_work",
        workspace_id=workspace_id,
        actor_id=actor_id,
        surface=surface,
        consult_warden=False,
    )
    if not decision.allowed:
        raise Denied(decision)

    if source not in VALID_SOURCES:
        raise WorkflowError("invalid_source", f"Unknown source: {source}")
    step_list = list(steps or [])
    _validate_steps(step_list)
    status = "shadow" if shadow_mode else "draft"
    now = utcnow()
    row = Workflow(
        id=new_id("wfl"),
        workspace_id=workspace_id,
        name=name.strip()[:120] or "Untitled workflow",
        description=(description or "").strip()[:2000],
        trigger_json=json.dumps(trigger or {"kind": "manual"}),
        steps_json=json.dumps(step_list),
        owner_id=owner_id,
        sla_json="{}",
        on_failure_json=json.dumps({"policy": "halt", "notify": "owner"}),
        autonomy=autonomy,
        status=status,
        cost_cap_run_usd=0.15,
        cost_cap_month_usd=5.0,
        source=source,
        unmapped_json="[]",
        version=1,
        created_at=now,
        updated_at=now,
    )
    factory = db()
    with factory() as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        view = _workflow_view(row)

    audit(
        "workflow.created",
        workspace_id=workspace_id,
        actor_id=actor_id,
        actor_kind="person",
        surface=surface,
        target_ref=view.id,
        payload={"name": view.name, "status": view.status, "steps": len(step_list)},
    )
    return view


def list_workflows(workspace_id: str) -> list[WorkflowView]:
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(Workflow)
            .where(Workflow.workspace_id == workspace_id)
            .order_by(Workflow.updated_at.desc())
        ).all()
        return [_workflow_view(r) for r in rows]


def get_workflow(workflow_id: str) -> WorkflowView | None:
    factory = db()
    with factory() as session:
        row = session.get(Workflow, workflow_id)
        return _workflow_view(row) if row else None


def pause_workflow(
    workflow_id: str,
    *,
    reason: str | None = None,
    actor_id: str = LOCAL_PERSON_ID,
    surface: str = "api",
) -> WorkflowView:
    factory = db()
    with factory() as session:
        row = session.get(Workflow, workflow_id)
        if row is None:
            raise WorkflowError("not_found", "That workflow doesn't exist.")
        if row.status == "retired":
            raise WorkflowError("retired", "A retired workflow cannot be paused.")
        row.status = "paused"
        row.paused_reason = reason or "paused by founder"
        row.updated_at = utcnow()
        session.commit()
        session.refresh(row)
        view = _workflow_view(row)

    audit(
        "workflow.paused",
        workspace_id=view.workspace_id,
        actor_id=actor_id,
        actor_kind="person",
        surface=surface,
        target_ref=view.id,
        payload={"reason": view.paused_reason},
    )
    return view


def resume_workflow(
    workflow_id: str,
    *,
    actor_id: str = LOCAL_PERSON_ID,
    surface: str = "api",
) -> WorkflowView:
    factory = db()
    with factory() as session:
        row = session.get(Workflow, workflow_id)
        if row is None:
            raise WorkflowError("not_found", "That workflow doesn't exist.")
        if row.status != "paused":
            raise WorkflowError("not_paused", "Only a paused workflow can be resumed.")
        # Resume into shadow if it never left the trust ramp.
        row.status = "shadow"
        row.paused_reason = None
        row.updated_at = utcnow()
        session.commit()
        session.refresh(row)
        view = _workflow_view(row)

    audit(
        "workflow.resumed",
        workspace_id=view.workspace_id,
        actor_id=actor_id,
        actor_kind="person",
        surface=surface,
        target_ref=view.id,
    )
    return view


async def start_run(
    workflow_id: str,
    *,
    trigger_payload: dict[str, Any] | None = None,
    idempotency_key: str | None = None,
    shadow_mode: bool | None = None,
    actor_id: str = LOCAL_PERSON_ID,
    surface: str = "api",
) -> RunView:
    decision = await check(
        "request_work",
        workspace_id=_workspace_of(workflow_id),
        actor_id=actor_id,
        surface=surface,
        consult_warden=False,
    )
    if not decision.allowed:
        raise Denied(decision)

    factory = db()
    with factory() as session:
        flow = session.get(Workflow, workflow_id)
        if flow is None:
            raise WorkflowError("not_found", "That workflow doesn't exist.")
        if flow.status == "paused":
            raise WorkflowError("paused", "Resume the workflow before running it.")
        if flow.status == "retired":
            raise WorkflowError("retired", "A retired workflow cannot run.")

        key = idempotency_key or f"man:{actor_id}:{new_id('man')}"
        existing = session.scalars(
            select(WorkflowRun).where(
                WorkflowRun.workflow_id == workflow_id,
                WorkflowRun.idempotency_key == key,
            )
        ).first()
        if existing is not None:
            return _run_view(existing)

        shadow = is_shadow_run(
            workflow_status=flow.status,
            run_shadow=bool(shadow_mode) if shadow_mode is not None else False,
        )
        # Explicit shadow_mode=True forces shadow; False still respects workflow status.
        if shadow_mode is True:
            shadow = True
        elif shadow_mode is False and flow.status == "active":
            shadow = False

        now = utcnow()
        run = WorkflowRun(
            id=new_id("wfr"),
            workflow_id=flow.id,
            workflow_version=flow.version,
            engagement_id=None,
            trigger_payload_json=json.dumps(trigger_payload or {"kind": "manual"}),
            idempotency_key=key,
            status="shadow" if shadow else "running",
            shadow=1 if shadow else 0,
            simulated_writes_json="[]",
            outcome_json="{}",
            cost_usd=0.0,
            started_at=now,
        )
        session.add(run)
        session.commit()
        session.refresh(run)
        run_id = run.id
        workspace_id = flow.workspace_id

    audit(
        "workflow.run.started",
        workspace_id=workspace_id,
        actor_id=actor_id,
        actor_kind="person",
        surface=surface,
        target_ref=run_id,
        payload={"workflow_id": workflow_id, "shadow": shadow},
    )

    from app.workflows.runner import execute_run

    return await execute_run(run_id, actor_id=actor_id, surface=surface)


def list_runs(workflow_id: str, *, limit: int = 50) -> list[RunView]:
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(WorkflowRun)
            .where(WorkflowRun.workflow_id == workflow_id)
            .order_by(WorkflowRun.started_at.desc())
            .limit(max(1, min(limit, 200)))
        ).all()
        return [_run_view(r) for r in rows]


def get_run(run_id: str) -> RunView | None:
    factory = db()
    with factory() as session:
        row = session.get(WorkflowRun, run_id)
        return _run_view(row) if row else None


def _workspace_of(workflow_id: str) -> str:
    factory = db()
    with factory() as session:
        row = session.get(Workflow, workflow_id)
        if row is None:
            raise WorkflowError("not_found", "That workflow doesn't exist.")
        return row.workspace_id
