"""MCP tool implementations — thin wrappers over the same services as REST (41 §3)."""

from __future__ import annotations

from typing import Any

from app.workspaces.resolve import LOCAL_PERSON_ID

TOOL_SPECS: list[dict[str, Any]] = [
    {
        "name": "list_workspaces",
        "description": "List workspaces with kind and status. Read-only.",
        "write": False,
    },
    {
        "name": "get_status",
        "description": "One workspace: open engagements, blockers, next up, cost. Read-only.",
        "write": False,
    },
    {
        "name": "request_work",
        "description": "Create an engagement from text. Costs tokens; may need approval.",
        "write": True,
    },
    {
        "name": "get_engagement",
        "description": "Engagement status, plan, deliverables. Read-only.",
        "write": False,
    },
    {
        "name": "answer_question",
        "description": "Supply the pending clarification answer. Write; may resume work.",
        "write": True,
    },
    {
        "name": "approve",
        "description": "Approve a pending proposal/brief/workflow write. Write.",
        "write": True,
    },
    {
        "name": "list_opportunities",
        "description": "Idea Lab contents for a workspace. Read-only.",
        "write": False,
    },
    {
        "name": "run_workflow",
        "description": "Manually trigger a workflow run. Write; respects shadow mode.",
        "write": True,
    },
    {
        "name": "get_activity",
        "description": "Recent narrated activity lines. Read-only.",
        "write": False,
    },
    {
        "name": "request_secret",
        "description": (
            "Ask the founder for a catalogued secret via the secure form. "
            "Never pass the secret value. Returns env_refs and armed/pending only."
        ),
        "write": True,
    },
]


async def call_tool(
    name: str,
    arguments: dict[str, Any] | None = None,
    *,
    actor_id: str = LOCAL_PERSON_ID,
    surface: str = "mcp",
) -> dict[str, Any]:
    args = dict(arguments or {})
    handler = _HANDLERS.get(name)
    if handler is None:
        return {"error": {"code": "unknown_tool", "message": f"No tool named {name}"}}
    return await handler(args, actor_id=actor_id, surface=surface)


async def _list_workspaces(
    args: dict[str, Any], *, actor_id: str, surface: str
) -> dict[str, Any]:
    from app.workspaces.service import list_workspaces

    items = [w.to_dict() for w in list_workspaces(actor_id)]
    return {"items": items}


async def _get_status(
    args: dict[str, Any], *, actor_id: str, surface: str
) -> dict[str, Any]:
    from app.surfaces.status import workspace_status

    workspace_id = str(args["workspace_id"])
    return workspace_status(workspace_id)


async def _request_work(
    args: dict[str, Any], *, actor_id: str, surface: str
) -> dict[str, Any]:
    from app.engagements import service as eng

    view = await eng.create_engagement(
        workspace_id=str(args["workspace_id"]),
        text=str(args.get("text") or args.get("request") or ""),
        origin="mcp",
        requested_by=actor_id,
        surface=surface,
    )
    return view.to_dict()


async def _get_engagement(
    args: dict[str, Any], *, actor_id: str, surface: str
) -> dict[str, Any]:
    from app.engagements import service as eng

    view = eng.get_engagement(str(args["engagement_id"]))
    if view is None:
        return {"error": {"code": "not_found", "message": "No engagement with that id."}}
    return view.to_dict()


async def _answer_question(
    args: dict[str, Any], *, actor_id: str, surface: str
) -> dict[str, Any]:
    from app.engagements import service as eng
    from app.engagements.service import EngagementError

    try:
        view = await eng.answer_clarification(
            str(args["engagement_id"]),
            str(args.get("answer") or args.get("text") or ""),
            actor_id=actor_id,
            surface=surface,
        )
    except EngagementError as exc:
        return {
            "status": "pending",
            "error": {"code": getattr(exc, "code", "blocked"), "message": str(exc)},
        }
    return view.to_dict()


async def _approve(
    args: dict[str, Any], *, actor_id: str, surface: str
) -> dict[str, Any]:
    from app.approvals import service as approvals

    view = approvals.decide(
        str(args["approval_id"]),
        decision=str(args.get("decision") or "approved"),
        decided_by=actor_id,
        note=args.get("note"),
        surface=surface,
    )
    return view.to_dict()


async def _list_opportunities(
    args: dict[str, Any], *, actor_id: str, surface: str
) -> dict[str, Any]:
    from app.idealab import slate

    items = slate.list_opportunities(
        str(args["workspace_id"]), status=args.get("status")
    )
    return {"items": [i.to_dict() for i in items]}


async def _run_workflow(
    args: dict[str, Any], *, actor_id: str, surface: str
) -> dict[str, Any]:
    from app.workflows import service as workflows

    run = await workflows.start_run(
        str(args["workflow_id"]),
        trigger_payload=dict(args.get("trigger_payload") or {"kind": "manual"}),
        idempotency_key=args.get("idempotency_key"),
        shadow_mode=args.get("shadow_mode"),
        actor_id=actor_id,
        surface=surface,
    )
    return run.to_dict()


async def _request_secret(
    args: dict[str, Any], *, actor_id: str, surface: str
) -> dict[str, Any]:
    from app.secrets.service import (
        SecretSlotError,
        reject_value_in_tool_args,
        request_secret,
        resolve_slot_id,
    )

    try:
        reject_value_in_tool_args(args)
        slot_id = resolve_slot_id(args.get("slot_id"), args.get("env_key"), args.get("provider"))
    except SecretSlotError as exc:
        return {"error": {"code": exc.code, "message": exc.message}}
    return request_secret(
        slot_id,
        venture_id=args.get("venture_id"),
        workspace_id=args.get("workspace_id"),
        reason=str(args.get("reason") or ""),
        requested_by=f"mcp:{actor_id}",
    )


async def _get_activity(
    args: dict[str, Any], *, actor_id: str, surface: str
) -> dict[str, Any]:
    from app.surfaces.status import workspace_activity

    return workspace_activity(
        str(args["workspace_id"]),
        limit=int(args.get("limit") or 50),
        cursor=args.get("cursor"),
    )


_HANDLERS = {
    "list_workspaces": _list_workspaces,
    "get_status": _get_status,
    "request_work": _request_work,
    "get_engagement": _get_engagement,
    "answer_question": _answer_question,
    "approve": _approve,
    "list_opportunities": _list_opportunities,
    "run_workflow": _run_workflow,
    "get_activity": _get_activity,
    "request_secret": _request_secret,
}
