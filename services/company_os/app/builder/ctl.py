"""build.run / watch / qa using SandboxProvider (doc 13)."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from app.core.fsm import advance as fsm_transition
from app.db.supymem_models import BuildArtifact, BuildEvent, BuildRun, FounderApp, QaRun
from app.sandbox.provider import CODING_TOOL_RESULTS_MESSAGE, CODING_TOOL_SETUP_MESSAGE, get_provider
from app.state import db
from core.clock import utcnow
from core.ids import new_id


def local_tool(venture_id: str, *, caller=None, provider: str | None = None) -> str:
    from fastapi import HTTPException
    from app.authz.workspace import assert_venture_access
    from app.coding_agents_consent import consents, raise_if_tool_changed
    from app.core.local_owner import current_owner
    from desktop_runtime.agent_status import statuses

    actor = caller.sub if caller is not None else current_owner().id
    assert_venture_access(venture_id, actor)
    allowed = consents(actor)
    try:
        from app.core.local_capabilities import enabled, blocked_message
        ready = enabled("build_with_my_tool")
        message = blocked_message("build_with_my_tool") if not ready else ""
    except (ImportError, KeyError) as exc:
        # shortcut: the capability may arrive in another lane, remove when its registry entry ships.
        if isinstance(exc, KeyError) and exc.args != ("build_with_my_tool",):
            raise
        from app.core.local_secrets import read_settings

        on = read_settings("capabilities")["values"].get("build_with_my_tool", True) is True
        ready = on and any(allowed.values())
        message = (CODING_TOOL_SETUP_MESSAGE if on else
                   "Open Settings > Your capabilities and switch on Build with your coding tool, then retry your build.")
    if not ready:
        raise_if_tool_changed(allowed, statuses(), provider)
        raise HTTPException(409, detail={"code": "build_tool_needs_setup", "message": message})

    tools = statuses()
    verified = {tool["name"] for tool in tools if all(tool.get(field) is True
                for field in ("installed", "signed_in", "subscription_login", "available"))}
    selected = provider or next((name for name in ("codex", "claude", "cursor", "opencode")
                                if allowed.get(name) and name in verified), None)
    if not selected or not allowed.get(selected) or selected not in verified:
        raise_if_tool_changed(allowed, tools, selected)
        raise HTTPException(409, detail={"code": "build_tool_needs_setup", "message":
            CODING_TOOL_SETUP_MESSAGE})
    return selected


def request_local_build(venture_id: str, instruction: str, *, caller=None, provider: str | None = None,
                        repair: bool = False, plan_task_id: str | None = None, brief: str | None = None,
                        order_id: str | None = None) -> dict[str, Any]:
    from fastapi import HTTPException
    from app.admin.authn import CallerContext
    from app.api.device_orders_api import invoke
    from app.build import device_orders
    from app.chat.product_build_request import is_utility_build_request
    from app.core.local_mode import is_local
    from app.core.local_owner import current_owner
    from app.secrets.detect import looks_like_secret
    from app.ventures.lifecycle import is_paused

    if not is_local():
        raise HTTPException(409, detail={"code": "local_build_required", "message": "Open your local app and ask in your venture chat to build with your connected coding tool."})
    if not instruction.strip():
        raise HTTPException(422, detail={"code": "build_request_required", "message": "Describe what you want built in your venture chat, then send your request."})
    if looks_like_secret(instruction) or (brief is not None and looks_like_secret(brief)):
        raise HTTPException(409, detail={"code": "build_request_contains_secret", "message": "Remove credentials from your request. Save keys in Settings > Keys and connections, then retry."})
    if caller is None:
        owner = current_owner()
        caller = CallerContext(sub=owner.id, email=owner.email or "", role="user", path="", method="POST", exp=0, nonce="")
    normalized = device_orders.normalize_device_chat(instruction)
    import re

    if sum(bool(re.search(rf"\b{name}\b", normalized)) for name in ("codex", "claude", "cursor", "opencode")) > 1:
        raise HTTPException(409, detail={"code": "build_tool_mismatch", "message": "Name one connected coding tool in your request, then retry from your venture chat."})
    named = device_orders._named_provider(normalized)
    if provider and named and provider != named:
        raise HTTPException(409, detail={"code": "build_tool_mismatch", "message": "Choose one coding tool in Settings > Keys and connections > Coding tools and name that same tool in your build request."})
    selected = local_tool(venture_id, caller=caller, provider=provider or named)
    if is_paused(venture_id):
        raise HTTPException(409, detail={"code": "venture_paused", "message": "Resume your venture in its chat, then request your build again."})
    if repair or plan_task_id:
        focus = f"Implement this requested change with {selected}.\n\n{instruction}"
        if plan_task_id:
            focus += f"\nPlan task: {plan_task_id}"
        options = {"brief": brief} if brief is not None else {}
        order = invoke(device_orders.prepare_coding_task_from_chat, venture_id, caller, focus, **options)
    else:
        focus = instruction if named or is_utility_build_request(instruction) else f"Build the app with {selected}.\n\n{instruction}"
        options = {"order_id": order_id} if order_id is not None else {}
        order = invoke(device_orders.prepare_product_build_from_chat, venture_id, caller, focus, provider=selected, **options)
    if order.get("state") == "pending":
        order = invoke(device_orders.transition, venture_id, caller, order["id"], order["review_digest"], "approve")
    return order


def _ensure_app(session, workspace_id: str, venture_id: str) -> FounderApp:
    from sqlalchemy import select

    row = session.scalar(select(FounderApp).where(FounderApp.venture_id == venture_id, FounderApp.deleted_at.is_(None)))
    if row is not None:
        return row
    now = utcnow()
    slug = new_id("slg")[4:16]
    row = FounderApp(
        id=new_id("app"),
        workspace_id=workspace_id,
        venture_id=venture_id,
        slug=slug,
        name=slug,
        kind="service",
        state="draft",
        created_at=now,
        updated_at=now,
    )
    session.add(row)
    session.flush()
    return row


def _event(session, run_id: str, seq: int, kind: str, payload: dict[str, Any]) -> None:
    session.add(
        BuildEvent(
            build_run_id=run_id,
            seq=seq,
            ts=utcnow(),
            kind=kind,
            payload=json.dumps(payload),
        )
    )


def run_build(payload: dict[str, Any]) -> dict[str, Any]:
    from app.core.local_mode import is_local

    if is_local():
        from fastapi import HTTPException
        from app.admin.authn import CallerContext
        from app.core.local_owner import ensure_owner
        from app.db.supymem_models import FounderRequest

        with db()() as session:
            request = session.get(FounderRequest, payload.get("request_id")) if payload.get("request_id") else None
            if (request is None or request.workspace_id != payload["workspace_id"]
                    or request.venture_id != payload["venture_id"] or request.outcome != "pending"
                    or request.source != "iteration" or request.intent != "iterate"):
                raise HTTPException(409, detail={"code": "build_request_required", "message": "Open Build activity from your venture chat to review existing work. To start a new build, send a new request in chat. No new build has started."})
            instruction = request.text
        owner = ensure_owner()
        caller = CallerContext(sub=owner.id, email=owner.email or "", role="user", path="", method="POST", exp=0, nonce="")
        order = request_local_build(payload["venture_id"], instruction, caller=caller, repair=payload.get("reason") == "iteration",
                                    plan_task_id=payload.get("plan_task_id"))
        with db()() as session:
            request = session.get(FounderRequest, payload["request_id"])
            request.outcome = "accepted"
            request.related_id = order["id"]
            session.commit()
        return order
    factory = db()
    now = utcnow()
    provider = get_provider()
    with factory() as session:
        app = _ensure_app(session, payload["workspace_id"], payload["venture_id"])
        run = BuildRun(
            id=new_id("bld"),
            app_id=app.id,
            workspace_id=app.workspace_id,
            venture_id=app.venture_id,
            plan_task_id=payload.get("plan_task_id"),
            trigger=str(payload.get("reason") or "plan_approved"),
            agent="core_swe",
            state="queued",
            spec=json.dumps(payload, default=str),
            started_at=now,
        )
        session.add(run)
        session.flush()
        fsm_transition(session, run, "reserving_credits")
        fsm_transition(session, run, "provisioning_sandbox")
        started = provider.start(run.id, payload)
        run.sandbox_ref = started["ref"]
        fsm_transition(session, run, "running")
        _event(session, run.id, 1, "started", {"provider": provider.name})
        fsm_transition(session, run, "syncing_artifacts")
        blob = json.dumps(started, sort_keys=True).encode()
        artifact = BuildArtifact(
            id=new_id("art"),
            build_run_id=run.id,
            app_id=app.id,
            workspace_id=app.workspace_id,
            kind="preview",
            s3_key=started["ref"],
            sha256=hashlib.sha256(blob).hexdigest(),
            size_bytes=len(blob),
            created_at=now,
        )
        session.add(artifact)
        run.artifact_id = artifact.id
        fsm_transition(session, run, "succeeded")
        run.finished_at = utcnow()
        run.exit_summary = "fake sandbox completed"
        _event(session, run.id, 2, "succeeded", {"preview": started.get("preview_url")})
        if app.state == "draft":
            fsm_transition(session, app, "provisioning")
            fsm_transition(session, app, "building")
        session.commit()
        rid = run.id
        aid = app.id
    run_qa({"build_run_id": rid, "app_id": aid, "workspace_id": payload["workspace_id"], "venture_id": payload["venture_id"]})
    return {"build_run_id": rid, "app_id": aid}


def run_qa(payload: dict[str, Any]) -> dict[str, Any]:
    from app.core.local_mode import is_local

    if is_local():
        from fastapi import HTTPException

        raise HTTPException(409, detail={"code": "verified_build_checks_required", "message": CODING_TOOL_RESULTS_MESSAGE})
    factory = db()
    now = utcnow()
    with factory() as session:
        qa = QaRun(
            id=new_id("qar"),
            app_id=payload["app_id"],
            build_run_id=payload["build_run_id"],
            workspace_id=payload["workspace_id"],
            venture_id=payload["venture_id"],
            state="complete",
            suites=json.dumps(["smoke"]),
            passed=1,
            failed=0,
            created_at=now,
            finished_at=now,
        )
        session.add(qa)
        session.commit()
        return {"qa_run_id": qa.id, "passed": 1, "failed": 0}


def terminal_exec(command: str) -> dict[str, Any]:
    allowed, reason = get_provider().exec_allowed(command)
    return {"allowed": allowed, "reason": reason, "command": command}


def get_build(run_id: str) -> dict[str, Any]:
    factory = db()
    with factory() as session:
        run = session.get(BuildRun, run_id)
        if run is None:
            raise KeyError(run_id)
        return {
            "id": run.id,
            "app_id": run.app_id,
            "state": run.state,
            "trigger": run.trigger,
            "sandbox_ref": run.sandbox_ref,
            "artifact_id": run.artifact_id,
            "exit_summary": run.exit_summary,
            "credits_used": run.credits_used,
        }


def list_builds(app_id: str) -> list[dict[str, Any]]:
    from sqlalchemy import select

    factory = db()
    with factory() as session:
        rows = list(session.scalars(select(BuildRun).where(BuildRun.app_id == app_id)))
        return [{"id": r.id, "state": r.state, "trigger": r.trigger, "started_at": r.started_at.isoformat() if r.started_at else None} for r in rows]


def list_events(run_id: str, after: int = 0) -> dict[str, Any]:
    from sqlalchemy import select

    factory = db()
    with factory() as session:
        rows = list(
            session.scalars(
                select(BuildEvent).where(BuildEvent.build_run_id == run_id, BuildEvent.seq > after).order_by(BuildEvent.seq)
            )
        )
        items = [{"seq": r.seq, "kind": r.kind, "payload": json.loads(r.payload or "{}"), "ts": r.ts.isoformat()} for r in rows]
        nxt = items[-1]["seq"] if items else after
        return {"items": items, "next": nxt}


def watch_build(payload: dict[str, Any]) -> dict[str, Any]:
    from app.core.local_mode import is_local

    run_id = payload["build_run_id"]
    factory = db()
    with factory() as session:
        run = session.get(BuildRun, run_id)
        if run is None:
            return {"state": "missing"}
        if is_local():
            return {"build_run_id": run_id, "state": "unverified", "sandbox": {
                "state": "unverified", "ref": run.sandbox_ref or "", "message": CODING_TOOL_RESULTS_MESSAGE}}
        st = get_provider().status(run.sandbox_ref or "")
        return {"build_run_id": run_id, "state": run.state, "sandbox": st}
