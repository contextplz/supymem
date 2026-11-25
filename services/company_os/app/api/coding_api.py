"""Coding-agent consent, detection, and dispatch surface (docs 08 / 91 / 93)."""

from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from coding_agents.consent import has_consent, record_consent, require_consent
from coding_agents.detect import detect
from coding_agents.session import INTERACTIVE_PROVIDERS, build_control_argv, scrub_var_names
from app.config import data_root
from app.core.local_mode import is_local

router = APIRouter(tags=["coding"])


async def dispatch(**kwargs):
    from coding_agents.dispatcher import dispatch as dispatch_build

    return await dispatch_build(**kwargs)


class ConsentBody(BaseModel):
    adapter: str
    version: str = "unknown"
    candidate: str | None = None


class DispatchBody(BaseModel):
    venture_id: str
    prompt: str
    prefs: list[str] = Field(default_factory=lambda: ["opencode", "cursor", "claude", "codex", "inhouse"])
    kind: str = "code"
    mode: str = "auto"
    branch: str | None = None
    order_id: str | None = Field(default=None, pattern=r"^dwo_[0-9a-f]{32}$")


class SessionPlanBody(BaseModel):
    provider: str
    venture_id: str | None = None
    auto_approve: bool = False
    model: str | None = None
    mode: str = "interactive"
    prompt: str | None = None
    resume: str | None = None


@router.get("/coding/agents")
def list_agents():
    allowed = None
    if is_local():
        from app.coding_agents_consent import consents
        from app.core.local_owner import current_owner

        allowed = consents(current_owner().id)
    out = []
    for a in detect():
        out.append(
            {
                "name": a.name,
                "binary": a.binary,
                "version": a.version,
                "auth_mode": a.auth_mode,
                "consented": allowed.get(a.name, False) if allowed is not None else has_consent(a.name),
            }
        )
    return {"agents": out}


@router.post("/coding/consent")
def post_consent(body: ConsentBody):
    if is_local():
        from app.coding_agents_consent import set_consent
        from app.core.local_owner import current_owner

        return {"adapter": body.adapter, "consent": set_consent(current_owner().id, body.adapter, True, body.candidate or "")}
    rec = record_consent(body.adapter, body.version)
    return {
        "adapter": rec.adapter,
        "version": rec.version,
        "consented_at": rec.consented_at,
    }


@router.post("/coding/dispatch")
async def post_dispatch(body: DispatchBody, request: Request):
    if is_local():
        from app.admin.authn import verify_caller
        from app.build.dispatcher import request_local_order

        if (body.kind not in {"code", "product"} or body.mode != "auto" or body.branch is not None
                or (body.order_id is not None and body.kind != "product")):
            raise HTTPException(409, detail={"code": "guarded_build_required", "message":
                "Open your venture chat and request this change with your connected coding tool without a branch override, or ask for read-only advice instead. No build has started."})
        provider = body.prefs[0] if "prefs" in body.model_fields_set and body.prefs else None
        options = {"order_id": body.order_id} if body.order_id is not None else {}
        return request_local_order(body.venture_id, body.prompt, caller=verify_caller(request), provider=provider,
                                   repair=body.kind != "product", **options)
    repo = data_root() / "ventures" / body.venture_id / "repo"
    repo.mkdir(parents=True, exist_ok=True)
    branch = body.branch or f"wo/manual-{body.venture_id[:8]}"
    result = await dispatch(
        prompt=body.prompt,
        repo=repo,
        branch=branch,
        prefs=body.prefs,
        kind=body.kind,
        mode=body.mode,
    )
    if result.error == "consent_required":
        raise HTTPException(
            status_code=403,
            detail={"error": {"code": "consent_required", "message": "consent required"}},
        )
    return {
        "ok": result.ok,
        "session_id": result.session_id,
        "changed_files": result.changed_files,
        "error": result.error,
        "argv": result.argv,
        "budget": _budget_label(result.argv),
        "write_flag_used": result.write_flag_used,
        "preview_url": result.preview_url,
    }


@router.post("/coding/sessions/plan")
def plan_interactive_session(body: SessionPlanBody):
    """Return argv + cwd for a desktop PTY. Does not spawn. Cloud hosts must 403."""
    if is_local():
        raise HTTPException(409, detail={"error": {"code": "guarded_build_required", "message":
            "Open Settings > Keys and connections > Coding tools, connect your tool, then ask in your venture chat to build so you can watch and stop it safely."}})
    if os.environ.get("COMPANY_OS_ENV", "local") == "cloud":
        raise HTTPException(
            status_code=403,
            detail={"error": {"code": "cloud_forbids_byo", "message": "local CLIs are not spawned on hosted Company OS"}},
        )
    provider = (body.provider or "").strip().lower()
    if provider not in INTERACTIVE_PROVIDERS:
        raise HTTPException(
            status_code=400,
            detail={"error": {"code": "unsupported_provider", "message": f"not an interactive provider: {provider}"}},
        )
    detected = {a.name: a for a in detect()}
    agent = detected.get(provider)
    if agent is None:
        raise HTTPException(
            status_code=404,
            detail={"error": {"code": "not_on_path", "message": f"{provider} is not on PATH"}},
        )
    try:
        require_consent(provider)
    except PermissionError:
        raise HTTPException(
            status_code=403,
            detail={"error": {"code": "consent_required", "message": "consent required"}},
        ) from None
    cwd = None
    if body.venture_id:
        repo = data_root() / "ventures" / body.venture_id / "repo"
        repo.mkdir(parents=True, exist_ok=True)
        cwd = str(repo)
    try:
        argv = build_control_argv(
            provider,
            mode=body.mode,
            auto_approve=body.auto_approve,
            model=body.model,
            prompt=body.prompt,
            resume=body.resume,
            workspace=cwd,
            session_name=f"Supymem {body.venture_id}" if body.venture_id else "Supymem",
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail={"error": {"code": "bad_provider", "message": str(exc)}}) from exc
    kind = (body.mode or "acp").strip().lower()
    return {
        "ok": True,
        "provider": provider,
        "argv": argv,
        "binary": agent.binary,
        "cwd": cwd,
        "scrub_vars": scrub_var_names(),
        "auth_mode": agent.auth_mode,
        "auto_approve": body.auto_approve,
        "mode": kind,
        "transport": "stdio" if kind in {"acp", "control", "print"} else "pty",
        "budget": _budget_label(argv),
    }


def _budget_label(argv: list[str]) -> str:
    if not argv:
        return "unknown"
    head = argv[0]
    if head in {"cursor-agent", "agent"}:
        return "Cursor (your subscription)"
    if head == "claude":
        return "Claude Code (your subscription)"
    if head == "codex":
        return "Codex (your subscription)"
    if head == "hermes":
        return "Hermes (open source, this Mac)"
    if head == "opencode":
        return "OpenCode (your subscription)"
    if head == "cursor-sdk-cloud":
        return "Cursor Cloud Agent (your subscription, Cursor cloud)"
    if head == "codex-cloud":
        return "Codex Cloud (your subscription, OpenAI cloud)"
    if head == "claude-routine":
        return "Claude Routines (beta, your subscription, Anthropic cloud)"
    if head == "inhouse-coder":
        return "In-house (DeepSeek / platform tokens)"
    if head == "openhands-builder":
        return "In-house builder (OpenHands sandbox, platform tokens)"
    return head
