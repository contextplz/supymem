"""Real ToolBus handlers — catalog names call existing Company OS services."""

from __future__ import annotations

import contextvars
from typing import Any

from app.runtime.toolbus import ToolBus, toolbus

_ctx: contextvars.ContextVar[dict[str, Any]] = contextvars.ContextVar(
    "harness_tool_ctx", default={}
)


def tool_context(**kwargs: Any) -> contextvars.Token[dict[str, Any]]:
    cur = dict(_ctx.get() or {})
    cur.update({k: v for k, v in kwargs.items() if v is not None})
    return _ctx.set(cur)


def reset_tool_context(token: contextvars.Token[dict[str, Any]]) -> None:
    _ctx.reset(token)


def _venture_id() -> str:
    return str((_ctx.get() or {}).get("venture_id") or "")


def _workspace_id() -> str:
    return str((_ctx.get() or {}).get("workspace_id") or "")


def _agent_id() -> str:
    return str((_ctx.get() or {}).get("agent_id") or "harness")


async def _explore_web(args: dict[str, Any]) -> dict[str, Any]:
    from app.research.explore import explore_web

    query = str(args.get("query") or args.get("goal") or "").strip()
    return await explore_web(query)


def _list_agents(args: dict[str, Any]) -> dict[str, Any]:
    from app.org.summary import resolve_org

    org = resolve_org(_venture_id())
    agents = []
    for row in org.get("agents") or []:
        if not isinstance(row, dict):
            continue
        agents.append(
            {
                "id": row.get("id"),
                "name": row.get("name"),
                "role": row.get("role"),
                "title": row.get("title"),
                "status": row.get("status"),
            }
        )
    return {"agents": agents, "count": len(agents)}


def _emit_signal(args: dict[str, Any]) -> dict[str, Any]:
    from app.state import MEM
    from app.memory.working import update_working_value

    vid = _venture_id()
    note = str(args.get("note") or args.get("goal") or args or "").strip()[:500]
    key = f"harness_signals:{vid}"
    update_working_value(MEM, key, lambda rows: [*(rows or []), note][-20:])
    return {"recorded": True, "note": note}


def _summarize_runs(args: dict[str, Any]) -> dict[str, Any]:
    from app.planning.store import list_tasks

    vid = _venture_id()
    if not vid:
        return {"tasks": []}
    rows = list_tasks(vid, statuses=("open", "running", "done"))
    slim = [
        {"id": r.get("id"), "title": r.get("title"), "status": r.get("status")}
        for r in rows[:12]
    ]
    return {"tasks": slim, "count": len(slim)}


def _set_priority(args: dict[str, Any]) -> dict[str, Any]:
    from app.state import MEM

    vid = _venture_id()
    title = str(args.get("title") or "")
    priority = str(args.get("priority") or "normal")
    MEM[f"harness_priority:{vid}:{title[:80]}"] = priority
    return {"title": title, "priority": priority}


def _check_policy(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "decision": "allow",
        "capability": str(args.get("capability") or ""),
        "note": "Warden already allowed this tool call.",
    }


def _propose(kind: str, args: dict[str, Any]) -> dict[str, Any]:
    summary = str(args.get("summary") or args.get("detail") or kind)[:200]
    wid = _workspace_id()
    if not wid:
        from app.state import MEM
        from app.memory.working import update_working_value

        key = f"harness_proposals:{_venture_id()}"
        update_working_value(MEM, key, lambda rows: [*(rows or []), {"kind": kind, "summary": summary}][-20:])
        return {"queued": True, "approval_id": None, "summary": summary, "store": "memory"}
    from app.approvals.service import request_approval

    view = request_approval(
        workspace_id=wid,
        kind="proposal",
        subject_ref=f"venture:{_venture_id()}",
        summary=summary,
        requested_by=_agent_id() or "harness",
        risk="medium",
        detail={"kind": kind, "detail": str(args.get("detail") or "")[:2000]},
    )
    return {"queued": True, "approval_id": view.id, "summary": view.summary}


def _propose_code(args: dict[str, Any]) -> dict[str, Any]:
    return _propose("propose_code", args)


def _propose_config(args: dict[str, Any]) -> dict[str, Any]:
    return _propose("propose_config", args)


def _run_oracle(args: dict[str, Any]) -> dict[str, Any]:
    return {"ok": False, "reason": "No live oracle is wired. Use explore_web for facts."}


def _run_rl_smoke(args: dict[str, Any]) -> dict[str, Any]:
    return {"ok": False, "reason": "No live RL runner is wired."}


def _ingest_feedback(args: dict[str, Any]) -> dict[str, Any]:
    from app.state import MEM
    from app.memory.working import update_working_value

    text = str(args.get("text") or "").strip()
    key = f"harness_feedback:{_venture_id()}"
    rows = update_working_value(MEM, key, lambda rows: [*(rows or []), *([text] if text else [])][-50:])
    return {"stored": bool(text), "count": len(rows)}


def _list_feedback(args: dict[str, Any]) -> dict[str, Any]:
    from app.state import MEM

    rows = list(MEM.get(f"harness_feedback:{_venture_id()}") or [])
    return {"notes": rows[-12:], "count": len(rows)}


def _approve_proposal(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": False,
        "reason": "A teammate cannot self-approve. Ask the founder in chat.",
    }


def _block_proposal(args: dict[str, Any]) -> dict[str, Any]:
    return {
        "ok": False,
        "reason": "A teammate cannot self-block. Ask the founder in chat.",
    }


def install_handlers(bus: ToolBus | None = None) -> ToolBus:
    target = bus or toolbus
    target.register("explore_web", _explore_web)
    target.register("list_agents", _list_agents)
    target.register("emit_signal", _emit_signal)
    target.register("summarize_runs", _summarize_runs)
    target.register("set_priority", _set_priority)
    target.register("check_policy", _check_policy)
    target.register("propose_code", _propose_code)
    target.register("propose_config", _propose_config)
    target.register("run_oracle", _run_oracle)
    target.register("run_rl_smoke", _run_rl_smoke)
    target.register("ingest_feedback", _ingest_feedback)
    target.register("list_feedback", _list_feedback)
    target.register("approve_proposal", _approve_proposal)
    target.register("block_proposal", _block_proposal)
    return target
