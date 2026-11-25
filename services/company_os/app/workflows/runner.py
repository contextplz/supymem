"""Execute one WorkflowRun step-by-step (40 §4)."""

from __future__ import annotations

import json
from typing import Any

from app.db.models import Connection, Workflow, WorkflowRun
from app.state import db
from app.workflows import shadow as shadow_mod
from app.workspaces.resolve import LOCAL_PERSON_ID
from core.clock import utcnow
from sqlalchemy import select


async def execute_run(
    run_id: str,
    *,
    actor_id: str = LOCAL_PERSON_ID,
    surface: str = "api",
):
    from app.workflows import service

    factory = db()
    with factory() as session:
        run = session.get(WorkflowRun, run_id)
        if run is None:
            raise service.WorkflowError("not_found", "That run doesn't exist.")
        flow = session.get(Workflow, run.workflow_id)
        if flow is None:
            raise service.WorkflowError("not_found", "That workflow doesn't exist.")
        steps = _steps(flow.steps_json)
        workspace_id = flow.workspace_id
        is_shadow = bool(run.shadow)
        run_status = run.status

    step_results: list[dict[str, Any]] = []
    simulated_writes: list[dict[str, Any]] = []
    failed = False
    fail_reason: str | None = None

    for step in steps:
        n = int(step.get("n") or (len(step_results) + 1))
        label = str(step.get("label") or f"Step {n}")
        action = step.get("action") or {}
        kind = str(action.get("kind") or "")
        try:
            result = await _dispatch(
                kind,
                action=action,
                step_n=n,
                label=label,
                workspace_id=workspace_id,
                is_shadow=is_shadow,
                simulated_writes=simulated_writes,
                actor_id=actor_id,
            )
            step_results.append(
                {"n": n, "label": label, "kind": kind, "ok": True, "result": result}
            )
            if result.get("blocked"):
                # human_task waits — mark blocked and stop.
                _finish(
                    run_id,
                    status="blocked",
                    step_results=step_results,
                    simulated_writes=simulated_writes,
                    reason=str(result.get("reason") or "waiting on a human"),
                )
                return service.get_run(run_id)  # type: ignore[return-value]
        except Exception as exc:  # noqa: BLE001 — surface as run failure
            failed = True
            fail_reason = str(exc)
            step_results.append(
                {
                    "n": n,
                    "label": label,
                    "kind": kind,
                    "ok": False,
                    "error": fail_reason,
                }
            )
            break

    status = "failed" if failed else ("shadow" if is_shadow else "succeeded")
    if status == "shadow" and run_status == "shadow":
        status = "succeeded"  # shadow run completed its simulation
        # Keep shadow flag; outcome notes it was a shadow execution.
    _finish(
        run_id,
        status="failed" if failed else "succeeded",
        step_results=step_results,
        simulated_writes=simulated_writes,
        reason=fail_reason,
        shadow=is_shadow,
    )

    from app.audit.writer import audit

    audit(
        "workflow.run.finished",
        workspace_id=workspace_id,
        actor_id=actor_id,
        actor_kind="person",
        surface=surface,
        target_ref=run_id,
        outcome="error" if failed else "ok",
        payload={
            "status": "failed" if failed else "succeeded",
            "shadow": is_shadow,
            "steps": len(step_results),
            "simulated_writes": len(simulated_writes),
        },
    )
    return service.get_run(run_id)  # type: ignore[return-value]


def _steps(raw: str) -> list[dict[str, Any]]:
    try:
        data = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return list(data) if isinstance(data, list) else []


def _finish(
    run_id: str,
    *,
    status: str,
    step_results: list[dict[str, Any]],
    simulated_writes: list[dict[str, Any]],
    reason: str | None = None,
    shadow: bool = False,
) -> None:
    factory = db()
    with factory() as session:
        run = session.get(WorkflowRun, run_id)
        if run is None:
            return
        run.status = status
        run.finished_at = utcnow()
        run.simulated_writes_json = json.dumps(simulated_writes)
        outcome: dict[str, Any] = {
            "steps": step_results,
            "shadow": shadow,
            "simulated_summary": shadow_mod.summarize(simulated_writes),
        }
        if reason:
            outcome["reason"] = reason
        run.outcome_json = json.dumps(outcome)
        session.commit()


async def _dispatch(
    kind: str,
    *,
    action: dict[str, Any],
    step_n: int,
    label: str,
    workspace_id: str,
    is_shadow: bool,
    simulated_writes: list[dict[str, Any]],
    actor_id: str,
) -> dict[str, Any]:
    if kind == "rule":
        return _run_rule(action)
    if kind == "human_task":
        return {
            "blocked": True,
            "reason": str(action.get("prompt") or "A human needs to take this step."),
        }
    if kind == "agent":
        # Stub: no live model call (doc 48 §6 / cost discipline).
        return {
            "stub": True,
            "capability": action.get("capability"),
            "prompt_ref": action.get("prompt_ref"),
            "note": "Agent step acknowledged (stub).",
        }
    if kind == "connector_read":
        return await _connector_read(action, workspace_id=workspace_id, actor_id=actor_id)
    if kind == "connector_write":
        if is_shadow:
            entry = shadow_mod.record_intended_write(
                simulated_writes,
                step_n=step_n,
                label=label,
                provider=str(action.get("provider") or ""),
                op=str(action.get("op") or action.get("capability") or ""),
                args=dict(action.get("args") or {}),
                target=action.get("target") or action.get("channel_ref"),
            )
            return {"shadow": True, "intended": entry}
        # Non-shadow writes are out of scope for the minimal runner; refuse.
        raise RuntimeError(
            "connector_write requires an active non-shadow workflow and approval path"
        )
    raise RuntimeError(f"unknown action kind: {kind}")


def _run_rule(action: dict[str, Any]) -> dict[str, Any]:
    """Deterministic comparison over declared vars. No free-form eval."""
    expr = str(action.get("expr") or "true")
    ctx = {str(k): v for k, v in dict(action.get("vars") or {}).items()}
    matched = _eval_simple(expr, ctx)
    return {"matched": matched, "expr": expr}


def _eval_simple(expr: str, ctx: dict[str, Any]) -> bool:
    text = expr.strip()
    lower = text.lower()
    if lower in {"true", "1", "yes"}:
        return True
    if lower in {"false", "0", "no"}:
        return False
    for op in (">=", "<=", "==", "!=", ">", "<"):
        if op not in text:
            continue
        left, right = text.split(op, 1)
        lv = _coerce(left.strip(), ctx)
        rv = _coerce(right.strip(), ctx)
        if op == ">=":
            return lv >= rv
        if op == "<=":
            return lv <= rv
        if op == "==":
            return lv == rv
        if op == "!=":
            return lv != rv
        if op == ">":
            return lv > rv
        if op == "<":
            return lv < rv
    # Fallback: truthiness of a named var.
    if text in ctx:
        return bool(ctx[text])
    return False


def _coerce(token: str, ctx: dict[str, Any]) -> Any:
    if token in ctx:
        return ctx[token]
    if token.startswith(("'", '"')) and token.endswith(("'", '"')) and len(token) >= 2:
        return token[1:-1]
    try:
        if "." in token:
            return float(token)
        return int(token)
    except ValueError:
        return token


async def _connector_read(
    action: dict[str, Any], *, workspace_id: str, actor_id: str
) -> dict[str, Any]:
    provider = str(action.get("provider") or "github")
    kind = str(action.get("resource") or action.get("kind") or "repo")
    factory = db()
    with factory() as session:
        row = session.scalars(
            select(Connection).where(
                Connection.workspace_id == workspace_id,
                Connection.provider == provider,
                Connection.status == "active",
            )
        ).first()
        if row is None:
            return {
                "skipped": True,
                "reason": f"no active {provider} connection",
            }
        from app.connectors.service import row_to_view

        con = row_to_view(row)

    from app.connectors import access, events
    from app.connectors.registry import get_connector

    connector = get_connector(provider, simulated=con.simulated)
    try:
        ticket = await access.authorize_read(
            con,
            connector,
            kind,
            actor_id=actor_id,
            surface="internal",
        )
        page = await connector.list_resources(con, kind, ticket)
    except Exception as exc:  # noqa: BLE001
        return {"error": str(exc), "provider": provider, "kind": kind}

    events.record(
        connection_id=con.id,
        workspace_id=workspace_id,
        direction="read",
        operation=f"list_{kind}",
        actor=actor_id,
        outcome="ok",
        target=con.external_account,
        bytes_in=page.bytes_in,
        simulated=con.simulated,
    )
    return {
        "provider": provider,
        "kind": kind,
        "count": len(page.items),
        "items": [
            {"ref": i.ref, "display": i.display, "kind": i.kind} for i in page.items
        ],
        "simulated": con.simulated,
    }
