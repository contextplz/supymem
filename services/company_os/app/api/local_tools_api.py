"""Health panel: coding agents + local LLM detection (docs 91 / 93 D5.4)."""

from __future__ import annotations

import json
import urllib.error
import urllib.request

from fastapi import APIRouter, HTTPException, Response

from app.api.coding_api import list_agents

router = APIRouter(tags=["local-tools"])


@router.get("/local-tools/coding-agents")
def local_coding_agents(response: Response, refresh: bool = False):
    from app.core.local_mode import is_local

    if not is_local():
        raise HTTPException(404, detail="Open your local app, then open Settings > Keys and connections > Coding tools.")
    from desktop_runtime.agent_status import statuses

    response.headers["Cache-Control"] = "no-store"
    return {"tools": [{"name": agent["name"], "label": agent["name"], "kind": "ide",
        "installed": agent["installed"], "connected": agent["available"],
        "signed_in": agent["signed_in"], "coding_available": agent["available"],
        "cli_path": agent["name"] if agent["installed"] else None, "app_path": None,
        **{key: agent[key] for key in ("unsafe_location", "unsafe_location_message", "candidate_path", "candidate", "build_blocked_message") if key in agent}}
        for agent in (statuses(refresh=True) if refresh else statuses())]}


def _probe_ollama(base: str = "http://127.0.0.1:11434") -> dict:
    try:
        with urllib.request.urlopen(f"{base}/api/tags", timeout=1.5) as resp:
            body = json.loads(resp.read().decode("utf-8") or "{}")
        models = [m.get("name") for m in (body.get("models") or []) if m.get("name")]
        return {"ok": True, "base_url": f"{base}/v1", "models": models[:20]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "base_url": f"{base}/v1", "error": str(exc), "models": []}


def _probe_lm_studio(base: str = "http://127.0.0.1:1234") -> dict:
    try:
        with urllib.request.urlopen(f"{base}/v1/models", timeout=1.5) as resp:
            body = json.loads(resp.read().decode("utf-8") or "{}")
        models = [m.get("id") for m in (body.get("data") or []) if m.get("id")]
        return {"ok": True, "base_url": f"{base}/v1", "models": models[:20]}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "base_url": f"{base}/v1", "error": str(exc), "models": []}


def _agent_reach_meta() -> dict:
    try:
        from app.research.agent_reach_bridge import meta

        return meta()
    except Exception as exc:  # noqa: BLE001
        return {"vendored": False, "error": str(exc)[:120]}


@router.get("/local-tools/health")
def local_tools_health():
    agents = list_agents()["agents"]
    ollama = _probe_ollama()
    lm = _probe_lm_studio()
    openworker: dict = {"vendored": False}
    try:
        from app.research.openworker_bridge import openworker_meta

        openworker = openworker_meta()
    except Exception as exc:  # noqa: BLE001
        openworker = {"vendored": False, "error": str(exc)[:120]}
    return {
        "coding_agents": agents,
        "local_llm": {
            "ollama": ollama,
            "lm_studio": lm,
            "any_available": bool(ollama.get("ok") or lm.get("ok")),
        },
        "openworker": openworker,
        "agent_reach": _agent_reach_meta(),
        "explore": {
            "loop": "hermes_search_then_extract",
            "search": "firecrawl|exa|tavily|openworker",
            "extract": "agent-reach specialists | openworker_fetch | jina",
            "vendors": {
                "agent-reach": "vendor/agent-reach (MIT, 15-platform router)",
                "hermes-agent": "vendor/hermes-agent (MIT, study + contract)",
                "openclaw": "vendor/openclaw (MIT, browser sidecar later)",
                "picoclaw": "vendor/picoclaw (MIT, Go sidecar later)",
                "huginn": "vendor/huginn (MIT, event graph) → packages/huginn_watch",
                "gbrain": "vendor/gbrain (MIT, study + contract) → app/gbrain",
            },
        },
        "gbrain": {
            "loop": "memory_verbs_then_think",
            "verbs": [
                "recall",
                "remember",
                "entity",
                "synthesize",
                "forget",
                "context_pack",
                "delta",
            ],
            "package": "services/company_os/app/gbrain",
            "upstream": "https://github.com/garrytan/gbrain",
        },
        "watch": {
            "loop": "huginn_check_then_propagate",
            "agents": [
                "WebsiteAgent",
                "RssAgent",
                "WebhookAgent",
                "TriggerAgent",
                "ChangeDetectorAgent",
                "DeDuplicationAgent",
                "PeakDetectorAgent",
                "DigestAgent",
            ],
            "package": "packages/huginn_watch",
        },
        "social": _social_health(),
    }


def _social_health(venture_id: str | None = None) -> dict:
    import os
    from app.actuation.social import env_social_key, resolve_social_key

    credential = resolve_social_key(venture_id) if venture_id else env_social_key()
    key = bool(credential)
    dry = os.environ.get("CHANNELS_DRY_RUN", "true").lower() != "false"
    sandbox = os.environ.get("SANDBOX_MODE", "true").strip().lower() in {"1", "true", "yes", "on"}
    blocker = None
    if dry:
        blocker = "CHANNELS_DRY_RUN is on, posts stay simulated"
    elif not key:
        blocker = "Connect posting for this venture in the console."
    return {
        "provider": credential[0] if credential else "zernio",
        "configured": key,
        "dry_run": dry,
        "sandbox": sandbox,
        "live_ready": bool(key and not dry and not sandbox),
        "blocker": blocker,
        "connect": "https://zernio.com/dashboard/api-keys",
        "docs": "https://docs.zernio.com",
    }
