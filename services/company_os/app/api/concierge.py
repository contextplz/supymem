"""Human Task Concierge API."""

from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from app.api.schemas import ConciergeChatBody, ConnectSubmitBody, CredentialBody
from app.concierge.chat import respond
from app.concierge.planner import create_task, list_open
from app.concierge.verify import store_in_vault_handle, validate_stripe_key
from app.core.local_mode import is_local
from app.db.models import Venture
from app.state import MEM, db

router = APIRouter(tags=["concierge"])


class AlternativeBody(BaseModel):
    reason: str = Field(default="", max_length=1000)


def _require_connect_provider(provider: str) -> None:
    if is_local() and provider == "stripe":
        from app.api.billing_api import local_payment_guidance

        local_payment_guidance()


@router.get("/ventures/{venture_id}/human-tasks")
def get_human_tasks(venture_id: str):
    from app.concierge.human import list_open_human_tasks

    durable = list_open_human_tasks(venture_id)
    catalog = list_open(venture_id)
    seen = {item.get("id") for item in durable}
    merged = durable + [item for item in catalog if item.get("id") not in seen]
    return {"items": merged}


@router.post("/ventures/{venture_id}/human-tasks/{task_id}/alternative")
def alternative_for_human_task(
    venture_id: str, task_id: str, payload: AlternativeBody | None = None
):
    from app.concierge.human import request_alternative

    try:
        task = request_alternative(
            venture_id, task_id, reason=(payload.reason if payload else "")
        )
    except KeyError as exc:
        raise HTTPException(404, "human_task_not_found") from exc
    return {"ok": True, "task": task}


@router.post("/ventures/{venture_id}/human-tasks")
def create_human_task(venture_id: str, payload: dict | None = None):
    factory = db()
    with factory() as session:
        if not session.get(Venture, venture_id):
            raise HTTPException(404, "not_found")
    catalog = (payload or {}).get("catalog_id") or "stripe"
    _require_connect_provider(catalog)
    try:
        task = create_task(catalog, venture_id=venture_id)
    except RuntimeError as e:
        raise HTTPException(409, str(e)) from e
    except FileNotFoundError as e:
        raise HTTPException(404, f"unknown_catalog:{catalog}") from e
    return task


@router.post("/ventures/{venture_id}/human-tasks/chat")
def human_task_chat(venture_id: str, payload: ConciergeChatBody):
    tasks = list_open(venture_id)
    last = None
    if tasks:
        steps = tasks[0].get("steps") or []
        if payload.step_id:
            last = next((s for s in steps if s.get("id") == payload.step_id), None)
        last = last or (steps[0] if steps else None)
    return respond(payload.message, last_step=last)


@router.post("/ventures/{venture_id}/human-tasks/credential")
def submit_credential(venture_id: str, payload: CredentialBody):
    """Validate format then return vault handle — never echo secret."""
    _require_connect_provider(payload.catalog)
    if payload.catalog == "stripe":
        result = validate_stripe_key(payload.value, kind=payload.kind)
        if not result.ok:
            raise HTTPException(400, {"code": result.reason})
        stored = store_in_vault_handle(payload.value, result.vault_handle or "vault:x")
        # Record handle only
        handles = MEM.setdefault(f"vault_handles:{venture_id}", [])
        handles.append({"handle": stored["handle"], "kind": payload.kind})
        return {"ok": True, "handle": stored["handle"]}
    raise HTTPException(400, "unsupported_catalog")


@router.get("/ventures/{venture_id}/connect/{provider}")
def get_connect_guide(venture_id: str, provider: str):
    """Steps, outbound links, and fields so the founder can unblock parked work."""
    _require_connect_provider(provider)
    from app.concierge.human import GUIDES, get_human_task

    factory = db()
    with factory() as session:
        if not session.get(Venture, venture_id):
            raise HTTPException(404, "not_found")
    guide = GUIDES.get(provider)
    if not guide:
        raise HTTPException(404, f"unknown_provider:{provider}")
    task = get_human_task(venture_id, provider=provider)
    return {
        "provider": provider,
        "guide": guide,
        "task": task,
        "href": f"/v/{venture_id}/connect/{provider}",
    }


@router.post("/ventures/{venture_id}/connect/{provider}")
async def submit_connect(venture_id: str, provider: str, payload: ConnectSubmitBody):
    """Save what the founder pasted, then unpark waiting tasks when we can continue."""
    _require_connect_provider(provider)
    from app.concierge.human import (
        GUIDES,
        complete_human_task,
        ensure_human_task,
    )

    factory = db()
    with factory() as session:
        ven = session.get(Venture, venture_id)
        if not ven:
            raise HTTPException(404, "not_found")
        workspace_id = ven.workspace_id
    if provider not in GUIDES:
        raise HTTPException(404, f"unknown_provider:{provider}")
    fields = dict(payload.fields or {})
    from app.secrets.catalog import slot_for_provider
    from app.config import is_local_env
    from app.secrets.dotenv_file import env_is_set

    spec = slot_for_provider(provider)
    # Hosted: platform env keys are not the founder's, so they never count as "already armed".
    armed = bool(spec and is_local_env() and all(env_is_set(k) for k in spec.env_keys))
    if not payload.skipped and provider == "social":
        if not armed and not any(
            str(fields.get(k) or "").strip() for k in ("token", "x", "instagram", "linkedin")
        ):
            raise HTTPException(400, "need_handles")
    if not payload.skipped and provider == "github" and not str(fields.get("token") or "").strip() and not armed:
        raise HTTPException(400, "need_token")
    if not payload.skipped and provider == "stripe":
        connected = False
        if workspace_id and not is_local():
            try:
                from app.payments.connect import status_for_workspace

                connected = bool(status_for_workspace(workspace_id).get("onboarding_complete"))
            except Exception:
                connected = False
        if (
            not armed
            and not connected
            and not str(fields.get("token") or "").strip()
            and not str(fields.get("connect") or "").strip()
        ):
            raise HTTPException(400, "need_token")
    if provider == "stripe" and fields.get("token") and not payload.skipped:
        result = validate_stripe_key(fields["token"], kind="secret")
        if not result.ok:
            raise HTTPException(400, {"code": result.reason})
    token = str(fields.get("token") or "").strip() or None
    if token and not payload.skipped and spec is not None:
        from app.secrets.service import SecretSlotError, save_secret

        try:
            save_secret(spec.id, {spec.env_keys[0]: token}, workspace_id=workspace_id)
        except SecretSlotError as exc:
            raise HTTPException(409, {"code": exc.code, "message": exc.message}) from exc
    try:
        out = complete_human_task(
            venture_id,
            provider=provider,
            fields=fields,
            skipped=payload.skipped,
        )
    except KeyError:
        ensure_human_task(
            venture_id,
            title=str((GUIDES[provider].get("open_label") or provider)),
            why="Needed so parked work can continue.",
            provider=provider,
        )
        out = complete_human_task(
            venture_id,
            provider=provider,
            fields=fields,
            skipped=payload.skipped,
        )
    fields.pop("token", None)
    if provider == "github" and workspace_id and not payload.skipped:
        try:
            from app.connectors.service import create_connection

            await create_connection(
                workspace_id=workspace_id,
                provider="github",
                auth_kind="pat",
                token=token,
                surface="api",
            )
        except Exception:  # noqa: BLE001 — vault handle is enough; connection is extra
            pass
    return {"ok": True, "task": out, "skipped": payload.skipped}


# Back-compat aliases for older console pages that still hit /tasks for concierge.
@router.post("/ventures/{venture_id}/tasks/chat")
def task_chat_legacy(venture_id: str, payload: ConciergeChatBody):
    return human_task_chat(venture_id, payload)


@router.post("/ventures/{venture_id}/tasks/credential")
def submit_credential_legacy(venture_id: str, payload: CredentialBody):
    return submit_credential(venture_id, payload)
