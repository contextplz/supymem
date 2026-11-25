"""Historical GTM reads. Unquoted synchronous mutation entry points are retired."""

from __future__ import annotations

import json
from typing import Any

from core.clock import utcnow
from core.ids import new_id
from sqlalchemy import select

from app.db.models import GtmGodMessage, GtmGodPlan, GtmGodTask
from app.gtm.god.catalog import AGENTS
from app.gtm.god.diagnose import Evidence, SpawnSpec, assert_spawn_legal, diagnose, diagnose_live
from app.gtm.god.specialists import SpecialistError, run_specialist
from app.state import db


class GodError(ValueError):
    def __init__(self, code: str, status: int = 400) -> None:
        self.code = code
        self.status = status
        super().__init__(code)


def _loads(raw: str, fallback: Any) -> Any:
    try:
        return json.loads(raw or "")
    except json.JSONDecodeError:
        return fallback


def _dump(obj: Any) -> str:
    return json.dumps(obj, ensure_ascii=False)


def _plan_row(workspace_id: str) -> GtmGodPlan | None:
    from app.db.models import GtmGodRun
    with db()() as session:
        row = session.scalars(
            select(GtmGodPlan)
            .where(GtmGodPlan.workspace_id == workspace_id,
                GtmGodPlan.id.not_in(select(GtmGodRun.plan_id)))
            .order_by(GtmGodPlan.updated_at.desc())
        ).first()
        if row is not None:
            session.expunge(row)
        return row


def _collect_evidence(workspace_id: str, artifacts: dict[str, Any]) -> Evidence:
    from app.gtm.relationships.service import list_queue, onboarding_status

    ob = onboarding_status(workspace_id)
    q = list_queue(workspace_id)
    site = artifacts.get("site") or {}
    launched = artifacts.get("launched") or {}
    return Evidence(
        users_count=int(artifacts.get("users_count") or 0),
        icp_ready=bool(ob.get("icp") or artifacts.get("icp")),
        positioning_ready=bool(artifacts.get("positioning")),
        site_url=site.get("url"),
        domain=(artifacts.get("domain") or {}).get("host"),
        launched=bool(launched.get("published")),
        launch_posts=len(artifacts.get("launch_posts") or []),
        seo_ready=bool(artifacts.get("seo")),
        mailbox=(artifacts.get("mailbox") or {}).get("address"),
        gmail_connected=any(
            s.get("provider") == "gmail" and s.get("status") == "connected"
            for s in (ob.get("evidence_sources") or [])
        ),
        prospect_count=len(q.get("items") or []),
    )


def start_plan(
    workspace_id: str,
    *,
    idea: str,
    problem: str = "",
    who: str = "",
    users_count: int = 0,
    venture_id: str | None = None,
) -> dict[str, Any]:
    raise GodError("gtm_quote_required", 409)


def _replan(workspace_id: str, plan_id: str) -> None:
    with db()() as session:
        plan = session.get(GtmGodPlan, plan_id)
        if plan is None or plan.workspace_id != workspace_id:
            raise GodError("plan_not_found", 404)
        brief = _loads(plan.brief_json, {})
        artifacts = _loads(plan.artifacts_json, {})
        evidence = _collect_evidence(workspace_id, artifacts)
        prev = _loads(plan.diagnosis_json, {})
        diagnosis = diagnose_live(brief, evidence, workspace_id=workspace_id) if not prev.get("skill") else diagnose(brief, evidence)
        existing = {
            t.kind
            for t in session.scalars(
                select(GtmGodTask).where(GtmGodTask.plan_id == plan_id)
            )
        }
        now = utcnow()
        kind_to_id: dict[str, str] = {}
        for t in session.scalars(select(GtmGodTask).where(GtmGodTask.plan_id == plan_id)):
            kind_to_id[t.kind] = t.id

        now_kinds = {s.kind for s in diagnosis.now}
        spawned: list[GtmGodTask] = []
        for spec in [*diagnosis.now, *diagnosis.next, *diagnosis.later]:
            assert_spawn_legal(spec.kind)
            if spec.kind in existing:
                row = session.scalars(
                    select(GtmGodTask).where(
                        GtmGodTask.plan_id == plan_id,
                        GtmGodTask.kind == spec.kind,
                    )
                ).first()
                if (
                    row is not None
                    and row.status == "queued"
                    and spec.kind in now_kinds
                    and row.horizon != "now"
                ):
                    row.horizon = "now"
                continue
            task = _task_from_spec(workspace_id, plan_id, spec, kind_to_id, now)
            session.add(task)
            spawned.append(task)
            kind_to_id[spec.kind] = task.id
            existing.add(spec.kind)
        # Second pass: resolve depends_on from kinds to ids for newly added tasks
        for task in spawned:
            deps = _loads(task.depends_on_json, [])
            resolved = []
            for dep in deps:
                if dep in kind_to_id:
                    resolved.append(kind_to_id[dep])
                elif dep.startswith("gtm"):
                    resolved.append(dep)
            task.depends_on_json = _dump(resolved)
        if diagnosis.llm_used:
            arts = _loads(plan.artifacts_json, {})
            arts["god_llm"] = True
            plan.artifacts_json = _dump(arts)
        plan.diagnosis_json = _dump(diagnosis.to_dict())
        plan.status = "executing"
        plan.updated_at = now
        if prev.get("skill") != diagnosis.skill or prev.get("bottleneck") != diagnosis.bottleneck:
            log = _loads(plan.log_json, [])
            log.append(
                {
                    "at": now.isoformat(),
                    "event": "diagnosed",
                    "skill": diagnosis.skill,
                    "llm_used": diagnosis.llm_used,
                }
            )
            plan.log_json = _dump(log)
        session.commit()


def _task_from_spec(
    workspace_id: str,
    plan_id: str,
    spec: SpawnSpec,
    kind_to_id: dict[str, str],
    now,
) -> GtmGodTask:
    meta = AGENTS[spec.kind]
    deps = [kind_to_id[k] for k in spec.depends_on_kinds if k in kind_to_id]
    unresolved = [k for k in spec.depends_on_kinds if k not in kind_to_id]
    return GtmGodTask(
        id=new_id("gtmgt"),
        workspace_id=workspace_id,
        plan_id=plan_id,
        kind=spec.kind,
        owner_agent=meta["owner"],
        status="queued",
        horizon=spec.horizon,
        depends_on_json=_dump(deps + unresolved),
        input_json="{}",
        output_json="{}",
        human_instruction=None,
        evidence_tag="assumption",
        created_at=now,
        updated_at=now,
    )


def tick(workspace_id: str, *, max_tasks: int = 16) -> dict[str, Any]:
    raise GodError("gtm_quote_required", 409)


def _settle(plan_id: str) -> None:
    if _next_ready(plan_id) is not None:
        with db()() as session:
            row = session.get(GtmGodPlan, plan_id)
            if row is not None and row.status != "done":
                row.status = "executing"
                row.updated_at = utcnow()
                session.commit()
        return
    if _has_blocked(plan_id):
        with db()() as session:
            row = session.get(GtmGodPlan, plan_id)
            if row is not None:
                row.status = "waiting_human"
                row.updated_at = utcnow()
                session.commit()
        return
    with db()() as session:
        queued = session.scalars(
            select(GtmGodTask).where(
                GtmGodTask.plan_id == plan_id,
                GtmGodTask.status == "queued",
            )
        ).first()
        row = session.get(GtmGodPlan, plan_id)
        if row is None:
            return
        row.status = "executing" if queued is not None else "done"
        row.updated_at = utcnow()
        session.commit()


def _has_blocked(plan_id: str) -> bool:
    with db()() as session:
        row = session.scalars(
            select(GtmGodTask).where(
                GtmGodTask.plan_id == plan_id,
                GtmGodTask.status == "blocked_human",
            )
        ).first()
        return row is not None


def _next_ready(plan_id: str) -> GtmGodTask | None:
    with db()() as session:
        tasks = list(session.scalars(select(GtmGodTask).where(GtmGodTask.plan_id == plan_id)))
        done = {t.id for t in tasks if t.status == "done"}
        queued = [t for t in tasks if t.status == "queued" and t.horizon == "now"]
        for t in queued:
            deps = _loads(t.depends_on_json, [])
            if all(d in done or d in {x.kind for x in tasks if x.status == "done"} for d in deps):
                session.expunge(t)
                return t
        return None


def _run_one(workspace_id: str, plan: GtmGodPlan, task: GtmGodTask) -> None:
    now = utcnow()
    with db()() as session:
        row = session.get(GtmGodTask, task.id)
        if row is None:
            return
        row.status = "running"
        row.updated_at = now
        session.commit()
    artifacts = _loads(plan.artifacts_json, {})
    brief = _loads(plan.brief_json, {})
    inbox = _inbox_for(plan.id, task.owner_agent)
    try:
        result = run_specialist(
            task.kind,
            workspace_id=workspace_id,
            plan={
                "idea": plan.idea,
                "problem": plan.problem,
                "brief": brief,
            },
            artifacts=artifacts,
            venture_id=plan.venture_id,
            inbox=inbox,
        )
    except SpecialistError as exc:
        result = {"status": "failed", "output": {"error": exc.code}}
    except Exception as exc:  # noqa: BLE001 — specialist must not kill the director
        result = {"status": "failed", "output": {"error": str(exc)[:240]}}
    status = str(result.get("status") or "done")
    if status not in {"done", "blocked_human", "failed", "skipped"}:
        status = "done"
    with db()() as session:
        row = session.get(GtmGodTask, task.id)
        prow = session.get(GtmGodPlan, plan.id)
        if row is None or prow is None:
            return
        row.status = status
        row.output_json = _dump(result.get("output") or {})
        row.human_instruction = result.get("human_instruction")
        row.updated_at = utcnow()
        arts = _loads(prow.artifacts_json, {})
        arts.update(result.get("artifacts") or {})
        prow.artifacts_json = _dump(arts)
        log = _loads(prow.log_json, [])
        log.append(
            {
                "at": utcnow().isoformat(),
                "event": "task",
                "kind": task.kind,
                "status": status,
                "agent": task.owner_agent,
            }
        )
        prow.log_json = _dump(log)
        prow.updated_at = utcnow()
        for msg in result.get("messages") or []:
            session.add(
                GtmGodMessage(
                    id=new_id("gtmgm"),
                    workspace_id=workspace_id,
                    plan_id=plan.id,
                    task_id=task.id,
                    from_agent=task.owner_agent,
                    to_agent=str(msg.get("to") or "god"),
                    body=str(msg.get("body") or ""),
                    payload_json="{}",
                    created_at=utcnow(),
                )
            )
        session.commit()


def _inbox_for(plan_id: str, owner_agent: str) -> list[dict[str, str]]:
    with db()() as session:
        rows = list(
            session.scalars(
                select(GtmGodMessage)
                .where(
                    GtmGodMessage.plan_id == plan_id,
                    GtmGodMessage.to_agent.in_([owner_agent, "god"]),
                )
                .order_by(GtmGodMessage.created_at)
            )
        )
        return [{"from": r.from_agent, "to": r.to_agent, "body": r.body} for r in rows]


def complete_human(workspace_id: str, task_id: str, *, note: str = "") -> dict[str, Any]:
    raise GodError("gtm_quote_required", 409)


def _promote_or_finish(workspace_id: str, plan_id: str) -> None:
    with db()() as session:
        nexts = list(
            session.scalars(
                select(GtmGodTask).where(
                    GtmGodTask.plan_id == plan_id,
                    GtmGodTask.horizon == "next",
                    GtmGodTask.status == "queued",
                )
            )
        )
        if nexts:
            for t in nexts:
                t.horizon = "now"
            row = session.get(GtmGodPlan, plan_id)
            if row is not None:
                row.status = "executing"
                row.updated_at = utcnow()
            session.commit()
            return
        laters = list(
            session.scalars(
                select(GtmGodTask).where(
                    GtmGodTask.plan_id == plan_id,
                    GtmGodTask.horizon == "later",
                    GtmGodTask.status == "queued",
                )
            )
        )
        if laters:
            for t in laters[:4]:
                t.horizon = "now"
            row = session.get(GtmGodPlan, plan_id)
            if row is not None:
                row.status = "executing"
                row.updated_at = utcnow()
            session.commit()
            return
        row = session.get(GtmGodPlan, plan_id)
        if row is not None:
            row.status = "done"
            row.updated_at = utcnow()
            session.commit()


def get_status(workspace_id: str) -> dict[str, Any]:
    plan = _plan_row(workspace_id)
    if plan is None:
        return {"ok": False, "plan": None, "tasks": [], "messages": []}
    with db()() as session:
        tasks = list(
            session.scalars(
                select(GtmGodTask)
                .where(GtmGodTask.plan_id == plan.id)
                .order_by(GtmGodTask.created_at)
            )
        )
        messages = list(
            session.scalars(
                select(GtmGodMessage)
                .where(GtmGodMessage.plan_id == plan.id)
                .order_by(GtmGodMessage.created_at)
            )
        )
        return {
            "ok": True,
            "plan": {
                "id": plan.id,
                "status": plan.status,
                "idea": plan.idea,
                "problem": plan.problem,
                "brief": _loads(plan.brief_json, {}),
                "diagnosis": _loads(plan.diagnosis_json, {}),
                "artifacts": _loads(plan.artifacts_json, {}),
                "log": _loads(plan.log_json, []),
            },
            "tasks": [
                {
                    "id": t.id,
                    "kind": t.kind,
                    "owner_agent": t.owner_agent,
                    "status": t.status,
                    "horizon": t.horizon,
                    "human_instruction": t.human_instruction,
                    "evidence_tag": t.evidence_tag,
                    "output": _loads(t.output_json, {}),
                    "depends_on": _loads(t.depends_on_json, []),
                }
                for t in tasks
            ],
            "messages": [
                {
                    "id": m.id,
                    "from": m.from_agent,
                    "to": m.to_agent,
                    "body": m.body,
                    "task_id": m.task_id,
                    "created_at": m.created_at.isoformat() if m.created_at else None,
                }
                for m in messages
            ],
        }
