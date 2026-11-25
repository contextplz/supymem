"""Founder BYOK LLM settings routes.

Only a credential-broker handle is persisted.  Responses intentionally expose
configuration metadata and a mask, never the API key or its broker handle.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.audit.writer import audit
from app.core import providers_local
from app.core.local_mode import is_local
from app.llm import founder_provider
from app.llm import pricing as llm_pricing

router = APIRouter(tags=["llm-settings"])
router.include_router(providers_local.router)


def _local_view() -> dict[str, object]:
    try:
        status = providers_local.configured_providers()
    except providers_local.LocalProviderError as exc:
        raise HTTPException(422, str(exc)) from None
    selected = status["providers"].get(status["provider"], {})
    return {**status, "model": selected.get("models", {}).get("chat"), "base_url": selected.get("base_url"),
        "source": "founder_override", "api_key_set": selected.get("api_key_set", False), "api_key": None,
        "hosted": False, "badge": "Using your provider" if selected.get("configured") else "Set up your provider to start"}


class LLMSettingsBody(BaseModel):
    provider: str = Field(default="azure_foundry")
    model: str = Field(default=founder_provider.PLATFORM_DEFAULT_MODEL)
    base_url: str | None = None
    api_key: str | None = Field(default=None, repr=False)


def _view(config: founder_provider.ResolvedLLMConfig) -> dict[str, object]:
    """Founder-facing view. Platform Azure URL/key never leave the server."""
    if is_local():
        return _local_view()
    usage = llm_pricing.workspace_usage_summary(config.workspace_id)
    try:
        from app.credits.accounts import founder_snapshot_for_workspace

        credits = founder_snapshot_for_workspace(config.workspace_id)
        if credits is not None:
            usage = {**usage, "credits": credits}
    except Exception as exc:
        from app.chat.recovery import log_founder_exception

        log_founder_exception(exc, None, "llm.settings.usage")
    offer = founder_provider.platform_offer()
    if config.source != "founder_override":
        return {
            "provider": config.provider,
            "model": config.model,
            "base_url": None,
            "source": "platform_default",
            "api_key_set": False,
            "api_key": None,
            "models": [config.model],
            "usage": usage,
            "offer": offer,
            "badge": f"Using Supymem: {offer['label']}",
            "hosted": True,
        }
    models = founder_provider.DEFAULT_MODELS.get(config.provider, [config.model])
    return {
        "provider": config.provider,
        "model": config.model,
        "base_url": config.base_url,
        "source": config.source,
        "api_key_set": config.key_is_set,
        "api_key": "********" if config.key_is_set else None,
        "models": models,
        "usage": usage,
        "offer": offer,
        "badge": f"Using your {config.provider} key · {config.model}",
        "hosted": False,
    }


def _error(exc: founder_provider.FounderLLMConfigError) -> HTTPException:
    return HTTPException(
        status_code=422,
        detail={"error": {"code": "invalid_llm_settings", "message": str(exc)}},
    )


@router.get("/workspaces/{workspace_id}/llm-settings")
def get_llm_settings(workspace_id: str):
    if is_local():
        return _local_view()
    return _view(founder_provider.resolve_llm_config(workspace_id))


@router.get("/llm/usage")
def get_global_llm_usage():
    """Lifetime token meter for this machine (all offices)."""
    return llm_pricing.global_usage_summary()


@router.get("/workspaces/local/llm-catalog", dependencies=[Depends(providers_local._local_api)])
def get_local_llm_catalog():
    return {
        **providers_local.configured_providers(),
        "default_models": {**founder_provider.DEFAULT_MODELS,
            "ollama": founder_provider.DEFAULT_MODELS["local"], "lmstudio": founder_provider.DEFAULT_MODELS["local"]},
    }


@router.get("/workspaces/{workspace_id}/llm-catalog")
def get_llm_catalog(workspace_id: str):
    """Curated model list per provider (plan catalog)."""
    if is_local():
        return get_local_llm_catalog()
    return {"providers": founder_provider.DEFAULT_MODELS, "pricing": llm_pricing.pricing_table()}


@router.put("/workspaces/{workspace_id}/llm-settings")
def put_llm_settings(workspace_id: str, body: LLMSettingsBody):
    if is_local():
        try:
            if body.api_key is not None and not body.api_key.strip():
                providers_local.clear_settings()
            else:
                providers_local.save_settings(provider="azure_openai" if body.provider == "azure_foundry" else body.provider,
                    base_url=body.base_url, api_key=body.api_key,
                    models={"chat": body.model} if "model" in body.model_fields_set else {})
        except providers_local.LocalProviderError as exc:
            raise HTTPException(422, str(exc)) from None
        return _local_view()
    # An explicitly empty password field clears the whole BYOK override. Omitting
    # it preserves an existing brokered key only for the same provider and URL.
    if body.api_key is not None and not body.api_key.strip():
        founder_provider.clear_founder_config(workspace_id)
        resolved = founder_provider.resolve_llm_config(workspace_id)
        audit(
            "llm_settings.cleared",
            workspace_id=workspace_id,
            surface="api",
            target_ref=workspace_id,
            payload={"source": resolved.source},
        )
        return _view(resolved)

    try:
        founder_provider.set_founder_config(
            workspace_id=workspace_id,
            provider=body.provider,
            model=body.model,
            base_url=body.base_url,
            api_key=body.api_key,
        )
    except founder_provider.FounderLLMConfigError as exc:
        raise _error(exc) from exc

    resolved = founder_provider.resolve_llm_config(workspace_id)
    audit(
        "llm_settings.updated",
        workspace_id=workspace_id,
        surface="api",
        target_ref=workspace_id,
        payload={
            "provider": resolved.provider,
            "model": resolved.model,
            "base_url": resolved.base_url,
            "source": resolved.source,
            "api_key_set": resolved.key_is_set,
        },
    )
    return _view(resolved)


@router.delete("/workspaces/{workspace_id}/llm-settings")
def delete_llm_settings(workspace_id: str):
    if is_local():
        try:
            providers_local.clear_settings()
        except providers_local.LocalProviderError as exc:
            raise HTTPException(422, str(exc)) from None
        return _local_view()
    founder_provider.clear_founder_config(workspace_id)
    resolved = founder_provider.resolve_llm_config(workspace_id)
    audit(
        "llm_settings.cleared",
        workspace_id=workspace_id,
        surface="api",
        target_ref=workspace_id,
        payload={"source": resolved.source},
    )
    return _view(resolved)


@router.post("/workspaces/{workspace_id}/llm-settings/test")
async def test_llm_connection(workspace_id: str):
    """Ping the resolved provider with a tiny completion."""
    from llm_gateway.errors import ProviderCallError
    from llm_gateway.sole import (
        byok_chat_completion,
        env_chat_completion,
        workspace_llm_context,
    )

    messages = [{"role": "user", "content": "Reply with exactly: ok"}]
    try:
        if is_local():
            from app.credits.metered import llm

            providers_local.load_local_config()
            resp = await llm(messages, purpose="byok_chat_completion", workspace_id=workspace_id, max_tokens=256)
            text = (resp.choices[0].message.content or "").strip()
            if not text:
                raise ProviderCallError("empty_response", "Your provider returned no answer. Check your model and retry.")
            return {"ok": True}
        resolved = founder_provider.resolve_llm_config(workspace_id)
        cfg = founder_provider.to_provider_config(workspace_id)
        if resolved.source == "founder_override" and cfg is None:
            raise ProviderCallError("provider_error", "Connection test failed.")
        with workspace_llm_context(workspace_id):
            if cfg is not None:
                resp = await byok_chat_completion(
                    messages, config=cfg, max_tokens=256, workspace_id=workspace_id
                )
            else:
                resp = await env_chat_completion(
                    messages, max_tokens=256, workspace_id=workspace_id
                )
        text = ""
        try:
            text = (resp.choices[0].message.content or "").strip()
        except Exception as exc:
            from app.chat.recovery import log_founder_exception

            log_founder_exception(exc, None, "llm.connection_test.validate")
            text = ""
        if not text:
            raise ProviderCallError(
                "empty_response",
                "The provider returned no answer. The connection test did not complete.",
                retryable=True,
            )
        return {"ok": True}
    except providers_local.LocalProviderError as exc:
        raise HTTPException(422, {"error": {
            "code": "invalid_llm_settings",
            "message": "Your AI provider needs setup before you can test it.",
            "hint": str(exc),
        }}) from None
    except founder_provider.FounderLLMConfigError as exc:
        raise _error(exc) from exc
    except ProviderCallError as exc:
        code = exc.code if exc.code in _CONNECTION_ERRORS else "provider_error"
        raise HTTPException(
            status_code=502,
            detail={"error": {
                "code": code, "message": _CONNECTION_ERRORS[code], "hint": _hint_for(code)
            }},
        ) from exc
    except Exception as exc:
        from app.chat.recovery import log_founder_exception

        log_founder_exception(exc, None, "llm.connection_test")
        raise HTTPException(
            status_code=502,
            detail={
                "error": {
                    "code": "connection_test_failed",
                    "message": "Something went wrong on our side during your connection test. Please retry.",
                    "hint": "Retry the connection test. Your saved settings are unchanged.",
                }
            },
        ) from exc


_CONNECTION_ERRORS = {
    "bad_api_key": "The provider rejected this key.",
    "rate_limited": "The provider is busy. Try again in a moment.",
    "out_of_credits": "Your provider account needs credits or quota.",
    "provider_down": "The provider could not be reached.",
    "empty_response": "The provider returned no answer. The connection test did not complete.",
    "provider_error": "The connection test could not complete.",
}


def _hint_for(code: str) -> str:
    return {
        "bad_api_key": "Paste a valid key for this provider, then save and re-test.",
        "rate_limited": "Wait a moment or switch model/provider.",
        "out_of_credits": "Top up credits with your provider, or use another key.",
        "provider_down": "Provider may be down, retry or switch provider.",
        "empty_response": "Retry the connection test. If it persists, check the deployment's output-token settings.",
    }.get(code, "Verify provider, model, and key in Settings.")
