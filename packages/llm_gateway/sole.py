"""Sole LLM HTTP client factory — every live model call goes through here.

Rules:
- Default: API key and base URL come from process env (`LLM_API_KEY`, `LLM_BASE_URL`, …).
  That env provider is the *platform* AI service (today often DeepSeek/Foundry) — not a
  hard-coded vendor; operators change it via LLM_* env.
- Optional workspace BYOK: when a workspace has a founder override (any OpenAI-compatible
  provider), live calls for that workspace dial *that* key/model instead.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import AsyncIterator, Callable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from core.time_context import time_context
from pydantic import SecretStr

if TYPE_CHECKING:
    from openai import AsyncOpenAI

from llm_gateway.boundary import (
    BoundaryViolation,
    Decision,
    provider_label,
    resolve_filter,
)
from llm_gateway.errors import ProviderCallError, classify_provider_error
from llm_gateway.provider_foundry import (
    ProviderConfig,
    create_async_client,
    create_async_client_from_config,
    deployment_for,
    load_provider_config_from_env,
    local_mode_enabled,
    loopback_provider,
    redacted_status,
    resolve_live_model,
    sample_mode_enabled,
)
from llm_gateway.reliability import Reliability
from llm_gateway.routing import (
    VALID_SOL_EFFORTS,
    canonicalize_model_config,
    completion_token_key,
    concurrency_for,
    is_sol_model,
    resolve_purpose_config,
    resolve_tier,
    wire_api_mode,
)
from llm_gateway.usage import extract_token_usage

WorkspaceLLMResolver = Callable[[str], ProviderConfig | None]
UsageRecorder = Callable[..., None]
_workspace_resolver: WorkspaceLLMResolver | None = None
_usage_recorder: UsageRecorder | None = None
_active_workspace_id: ContextVar[str | None] = ContextVar(
    "llm_active_workspace_id", default=None
)
logger = logging.getLogger("llm_gateway.sole")
_reliabilities: dict[str, Reliability] = {}
_primary_chat: Callable[..., Any] | None = None
_fallback_chat: Callable[..., Any] | None = None


def __getattr__(name: str):
    if name not in {"APIConnectionError", "APIStatusError", "AsyncOpenAI", "RateLimitError"}:
        raise AttributeError(name)
    import openai

    value = getattr(openai, name)
    globals()[name] = value
    return value


async def _close_owned_client(client: AsyncOpenAI) -> None:
    """Await cleanup without replacing a provider receipt or an in-flight error."""
    try:
        await client.close()
    except Exception as exc:  # noqa: BLE001 - cleanup must preserve the paid outcome
        # The metered caller must still receive the known provider outcome.
        # Never log exception text/tracebacks: these may contain secrets or URLs.
        logger.warning("llm_client_cleanup_failed", extra={"error_type": type(exc).__name__})


def set_transmit_hooks(
    *,
    primary: Callable[..., Any] | None = None,
    fallback: Callable[..., Any] | None = None,
) -> None:
    """Tests replace Foundry/Bedrock dials without touching the network."""
    global _primary_chat, _fallback_chat
    _primary_chat = primary
    _fallback_chat = fallback


def set_reliability_for(tier: str, reliability: Reliability | None) -> None:
    if reliability is None:
        _reliabilities.pop(tier, None)
    else:
        _reliabilities[tier] = reliability


def reliability_for(tier: str) -> Reliability:
    cap = max(1, concurrency_for(tier))
    existing = _reliabilities.get(tier)
    if existing is None or existing.max_concurrency != cap:
        existing = Reliability(max_concurrency=cap)
        _reliabilities[tier] = existing
    return existing


def _resolved_tier(purpose: str, tier: str) -> str:
    declared = resolve_tier(purpose)
    # This registered compatibility facade historically owns its caller tier.
    # Specific product purposes keep the declarative routing contract.
    if purpose == "env_chat_completion":
        if tier not in {"pro", "flash"}:
            raise ValueError("Legacy chat completions require a text tier")
        return tier
    return declared


def _sol_effort(config):
    effort = config.reasoning_effort or "low"
    if effort not in VALID_SOL_EFFORTS:
        raise ValueError("Reasoning effort is unsupported by this deployment")
    return effort


def _completion_kwargs(messages, *, config, model, purpose, tools=None,
                       max_tokens=None, response_schema=None, parallel_tool_calls=None):
    from llm_gateway.messages import (
        json_schema_response_format,
        normalize_chat_messages,
        sanitize_chat_tools,
    )

    loopback = loopback_provider(config)
    sol = wire_api_mode(config, model) == "responses"
    token_key = "max_tokens" if loopback else "max_completion_tokens" if sol else completion_token_key(config.catalog_model or model)
    kwargs = {"model": model, "messages": messages if sol else normalize_chat_messages(messages),
              token_key: max_tokens or config.max_output_tokens}
    live_tools = sanitize_chat_tools(tools)
    if live_tools:
        kwargs.update(tools=live_tools, tool_choice="auto")
        if parallel_tool_calls is not None:
            kwargs["parallel_tool_calls"] = parallel_tool_calls
    if response_schema:
        kwargs["response_format"] = json_schema_response_format(response_schema,
            provider=config.provider, model=None if loopback else config.catalog_model or model)
        if kwargs["response_format"]["type"] == "json_object" and response_schema != {"type": "json_object"}:
            import json
            # Existing loose schemas use JSON mode plus local validation. Give
            # the model the same contract counted by the reservation estimator.
            contract = "Return only a JSON object matching this schema: " + json.dumps(response_schema, ensure_ascii=False, separators=(",", ":"))
            live_messages = list(kwargs["messages"])
            index = next((i for i, m in enumerate(live_messages) if m.get("role") != "system"), len(live_messages))
            live_messages.insert(index, {"role": "system", "content": contract})
            kwargs["messages"] = live_messages
    if sol:
        kwargs["reasoning_effort"] = _sol_effort(config)
    elif _bounded_structured_mode(config, model, purpose, response_schema, tools) or _chat_fast_mode(config, model, purpose, tools):
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    elif config.provider in {"azure_foundry", "deepseek_direct"} and config.reasoning_effort:
        kwargs["extra_body"] = {"reasoning_effort": config.reasoning_effort}
    return kwargs


async def _dial_chat(client, config, kwargs):
    started = time.monotonic()
    try:
        if wire_api_mode(config, kwargs["model"]) == "responses":
            from llm_gateway.responses import responses_chat
            # Every Sol attempt is owned by the caller's durable admission.
            return await responses_chat(client.with_options(max_retries=0), **kwargs)
        return await client.chat.completions.create(**kwargs)
    finally:
        _log_chat_call(purpose=config.routing_purpose, model=kwargs["model"], provider=config.provider,
            prompt=kwargs, started=started, config=config)


def _log_chat_call(*, purpose, model, provider, prompt, started, config=None):
    if purpose not in {"venture.chat.assistant", "venture.chat.route"}:
        return
    try:
        import json

        from core.redact import redact_string
        duration_ms = int((time.monotonic() - started) * 1000)
        prompt_token_estimate = len(json.dumps(prompt, ensure_ascii=False).encode("utf-8"))
        secrets = [secret.get_secret_value() for secret in (config.api_key, config._platform_api_key_pin)
            if secret is not None] if config is not None else []
        deployment = redact_string(str(model), exact_values=secrets)
        logger.info("chat_model_call duration_ms=%d prompt_token_estimate=%d deployment=%s provider=%s",
            duration_ms, prompt_token_estimate,
            deployment, provider, extra={"duration_ms": duration_ms,
                "prompt_token_estimate": prompt_token_estimate, "deployment": deployment,
                "provider": provider})
    except Exception:  # noqa: BLE001, S110 - latency telemetry must never break a chat reply
        pass


async def transmit_responses_request(body, *, purpose, config, workspace_id=None,
                                     segments=None, trace_id=None):
    """One captured raw Responses call for reviewed device capabilities.

    The capability owner admits/prices the complete request before calling and
    settles returned usage before validating output. Failures with a provider
    receipt remain visible; this helper never retries or supplies effect authority.
    """
    import json
    from copy import deepcopy

    from llm_gateway.sole import APIConnectionError, APIStatusError, RateLimitError

    cfg = resolve_purpose_config(purpose, config, tier=config.captured_tier)
    if wire_api_mode(cfg) != "responses" or cfg.source not in {"platform_default", "founder_override"}:
        raise ValueError("Raw Responses requires a captured supported route")
    if cfg.source != "founder_override":
        _require_platform_server()
        require_env_api_key(cfg)
    request = deepcopy(body)
    limit = request.get("max_output_tokens")
    if type(limit) is not int or not 1 <= limit <= cfg.max_output_tokens:
        raise ValueError("Responses request exceeds its captured output policy")
    inputs = request.get("input")
    if not isinstance(inputs, list) or any(not isinstance(m, dict) or m.get("role") not in {"system", "developer", "user", "assistant"} for m in inputs):
        raise ValueError("Raw capability input must contain explicit messages")
    decision = boundary_decision(inputs, purpose=purpose, workspace_id=workspace_id,
        provider=provider_label(cfg.captured_tier, config=cfg),
        segments=segments, trace_id=trace_id, tier=cfg.captured_tier)
    request.update(model=deployment_for(cfg, cfg.captured_tier),
        input=_time_messages(decision.messages), store=False,
        reasoning={"effort": _sol_effort(cfg)})
    client, _ = create_async_client_from_config(cfg)
    try:
        try:
            from llm_gateway.responses import responses_create, responses_to_chat
            response = await responses_create(client.with_options(max_retries=0), **request)
        except (APIStatusError, RateLimitError, APIConnectionError, TimeoutError) as exc:
            raise classify_provider_error(exc) from exc
        try:
            encoded = json.dumps(response, ensure_ascii=False, allow_nan=False).encode("utf-8")
        except (ValueError, UnicodeError):
            encoded = None
        normalized = responses_to_chat(response)
        _note_usage(normalized, workspace_id=workspace_id, model=cfg.catalog_model,
            purpose=purpose, source="byok" if cfg.source == "founder_override" else "platform")
        if encoded is None or len(encoded) > 200_000:
            # Preserve confirmed billing before the owner rejects this output.
            actual_model = response.get("model")
            actual_tier = response.get("service_tier")
            return {"id": str(response.get("id", ""))[:128],
                "model": actual_model if isinstance(actual_model, str) and len(actual_model) <= 256 else None,
                "service_tier": actual_tier if actual_tier is None or isinstance(actual_tier, str) and len(actual_tier) <= 32 else "unconfirmed",
                "status": "failed", "output": [],
                "usage": response.get("usage"),
                "error": {"code": "invalid_provider_output" if encoded is None else "response_too_large"},
                **({"provider_output_error": "invalid_output"} if encoded is None else {})}
        return normalized.responses_raw
    finally:
        await _close_owned_client(client)


def responses_request_prompt_tokens(body, *, purpose, config, clock_text, image_tokens=0):
    """Quote raw text/schema framing plus the owner's explicit image allowance.

    Base64/URL size is never an image-token estimate. The capability owner must
    validate image geometry and choose its reviewed token policy separately.
    """
    import json
    from copy import deepcopy
    if type(image_tokens) is not int or not 0 <= image_tokens <= 200_000:
        raise ValueError("Image token allowance must follow its capability policy")
    cfg = resolve_purpose_config(purpose, config, tier=config.captured_tier)
    if wire_api_mode(cfg) != "responses":
        raise ValueError("Raw Responses quote requires a matching protocol")
    request = deepcopy(body)
    inputs = request.get("input")
    if not isinstance(inputs, list):
        raise TypeError("Raw capability input must contain explicit messages")
    has_image = False
    for message in inputs:
        if not isinstance(message, dict) or message.get("role") not in {"system", "developer", "user", "assistant"}:
            raise ValueError("Raw capability input must contain explicit messages")
        if isinstance(message.get("content"), list):
            parts = []
            for part in message["content"]:
                if not isinstance(part, dict) or part.get("type") not in {"input_text", "output_text", "input_image"}:
                    raise ValueError("Unsupported raw capability content")
                if part["type"] == "input_image":
                    has_image = True
                else:
                    parts.append(part)
            message["content"] = parts
    if has_image and not image_tokens:
        raise ValueError("Image admission requires an explicit capability allowance")
    index = next((i for i, m in enumerate(inputs) if m.get("role") != "system"), len(inputs))
    inputs.insert(index, {"role": "system", "content": clock_text})
    request.update(model=deployment_for(cfg, cfg.captured_tier), store=False,
        reasoning={"effort": _sol_effort(cfg)})
    envelope = json.dumps(request, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return len(envelope.encode("utf-8")) + 128 + 16 * len(inputs) + image_tokens


def transport_prompt_tokens(messages, *, purpose, config, tools=None, response_schema=None,
                            clock_text=None):
    """Conservative admission allowance for the actual request, never billed usage.

    UTF-8 envelope bytes plus framing allowance cover schema/tools/metadata and
    the existing server clock. This does not modify or inject caller messages.
    """
    import json
    cfg = resolve_purpose_config(purpose, config, tier=config.captured_tier)
    estimated_messages = [dict(m) for m in messages]
    index = next((i for i, m in enumerate(estimated_messages) if m.get("role") != "system"), len(estimated_messages))
    estimated_messages.insert(index, {"role": "system", "content": clock_text if clock_text is not None else time_context()})
    kwargs = _completion_kwargs(estimated_messages, config=cfg,
        model=deployment_for(cfg, cfg.captured_tier), purpose=purpose,
        tools=tools, response_schema=response_schema)
    if wire_api_mode(cfg) == "responses":
        from llm_gateway.responses import responses_request_kwargs
        kwargs = responses_request_kwargs(**kwargs)
    for message in estimated_messages:
        if isinstance(message.get("content"), list) and any(isinstance(part, dict) and part.get("type") not in {"text", "input_text", "output_text"} for part in message["content"]):
            raise ValueError("Multimodal admission requires its capability token policy")
    envelope = json.dumps(kwargs, ensure_ascii=False, separators=(",", ":"), allow_nan=False)
    return len(envelope.encode("utf-8")) + 128 + 16 * len(estimated_messages)


def _local_completion(*, purpose: str, content: str = "") -> Any:
    msg = SimpleNamespace(content=content, tool_calls=None, reasoning_content=None)
    choice = SimpleNamespace(message=msg, finish_reason="stop")
    usage = SimpleNamespace(prompt_tokens=0, completion_tokens=0, total_tokens=0)
    return SimpleNamespace(
        choices=[choice],
        usage=usage,
        model="local",
        provider_used="local",
        purpose=purpose,
    )


def _sample_completion(purpose: str) -> Any:
    from llm_gateway.cassette import fixture_for

    fixture = fixture_for(purpose) or {"content": "{}"}
    response = _local_completion(purpose=purpose, content=fixture["content"])
    response.model = "sample"
    response.provider_used = "sample"
    return response


def set_workspace_llm_resolver(fn: WorkspaceLLMResolver | None) -> None:
    """Company OS registers BYOK resolution here at bootstrap."""
    global _workspace_resolver
    _workspace_resolver = fn


def set_usage_recorder(fn: UsageRecorder | None) -> None:
    """Optional hook to meter token usage after a successful call."""
    global _usage_recorder
    _usage_recorder = fn


def get_active_workspace_id() -> str | None:
    return _active_workspace_id.get()


@contextmanager
def workspace_llm_context(workspace_id: str | None) -> Iterator[None]:
    """Bind BYOK resolution to this workspace for the duration of the block."""
    token = _active_workspace_id.set((workspace_id or "").strip() or None)
    try:
        yield
    finally:
        _active_workspace_id.reset(token)


def resolve_override(
    workspace_id: str | None = None,
    *,
    config: ProviderConfig | None = None,
) -> ProviderConfig | None:
    """Return founder BYOK config when present; else None (use platform env)."""
    if config is not None and getattr(config, "source", None) == "founder_override":
        return config
    if local_mode_enabled():
        if config is not None and config.routing_purpose is not None:
            _require_platform_server()
        return load_provider_config_from_env()
    wid = (workspace_id or get_active_workspace_id() or "").strip() or None
    if not wid or _workspace_resolver is None:
        return None
    try:
        return _workspace_resolver(wid)
    except Exception as exc:
        # Provider exceptions may contain credentials or prompt text; log only their type.
        logger.exception("workspace_llm_resolution_failed", exc_info=(RuntimeError, RuntimeError(type(exc).__name__), None))
        return None


def note_usage(
    resp: Any,
    *,
    workspace_id: str | None,
    model: str,
    purpose: str,
    source: str | None = None,
) -> None:
    """Persist provider token counts when a recorder is registered.

    ``source`` is ``byok`` when the founder’s own API key was dialed, else
    ``platform``. Recorders that do not accept ``source`` still get the call.
    """
    if not _usage_recorder:
        return
    usage = getattr(resp, "usage", None)
    if usage is None and isinstance(resp, dict):
        usage = resp.get("usage")
    if is_sol_model(model) or getattr(resp, "responses_raw", None) is not None:
        from llm_gateway.usage import extract_confirmed_token_usage
        counts = extract_confirmed_token_usage(usage)
        if counts is None:
            return
    else:
        counts = extract_token_usage(usage)
    wid = (workspace_id or get_active_workspace_id() or "").strip() or "platform"
    billed_from = (source or "").strip() or (
        "byok" if "byok" in (purpose or "").lower() else "platform"
    )
    try:
        _usage_recorder(
            wid,
            model=model,
            prompt_tokens=counts["prompt_tokens"],
            completion_tokens=counts["completion_tokens"],
            cached_tokens=counts["cached_tokens"],
            reasoning_tokens=counts["reasoning_tokens"],
            purpose=purpose,
            source=billed_from,
        )
    except TypeError:
        try:
            _usage_recorder(
                wid,
                model=model,
                prompt_tokens=counts["prompt_tokens"],
                completion_tokens=counts["completion_tokens"],
                purpose=purpose,
            )
        except Exception as exc:
            logger.exception("legacy_usage_recording_failed", exc_info=(RuntimeError, RuntimeError(type(exc).__name__), None))
            return
    except Exception as exc:
        logger.exception("usage_recording_failed", exc_info=(RuntimeError, RuntimeError(type(exc).__name__), None))
        return


_note_usage = note_usage


def require_env_api_key(cfg: ProviderConfig) -> str:
    """Validate the original authorized platform key, or authorize an unpinned config."""
    if local_mode_enabled():
        _require_platform_server()
    if cfg._platform_api_key_pin is not None:
        pinned_key = cfg._platform_api_key_pin.get_secret_value().strip()
        from llm_gateway.provider_foundry import endpoint_host

        if (cfg.source != "platform_default" or not pinned_key or cfg.api_key.get_secret_value().strip() != pinned_key
                or endpoint_host(cfg.base_url) != cfg._platform_host_pin):
            raise RuntimeError("Refusing LLM call: captured platform credentials changed.")
        return pinned_key
    env_key = (os.environ.get("LLM_API_KEY") or "").strip()
    cfg_key = cfg.api_key.get_secret_value().strip()
    if not env_key:
        raise ProviderCallError(
            "model_not_configured",
            "Your model is not configured. Check your model settings, then retry.",
        )
    # The landing-only OpenAI route (pagebuilder) is the one other operator env key allowed here.
    if cfg_key and cfg_key not in {env_key, (os.environ.get("PAGEBUILDER_OPENAI_API_KEY") or "").strip()}:
        raise RuntimeError(
            "Refusing LLM call: ProviderConfig.api_key does not match LLM_API_KEY from the environment. "
            "Platform traffic must use the configured .env key (workspace BYOK uses a separate path)."
        )
    return env_key


def open_env_client(config: ProviderConfig | None = None) -> tuple[AsyncOpenAI, ProviderConfig]:
    """Open the shared platform client from env (operator-configured provider)."""
    if local_mode_enabled():
        return create_async_client(resolve_override(config=config))
    _require_platform_server()
    cfg = config or load_provider_config_from_env()
    require_env_api_key(cfg)
    live = load_provider_config_from_env()
    require_env_api_key(live)
    return create_async_client(live)


_LOCAL_PROVIDER_SETTINGS = "Settings > Keys and connections > Your AI provider"


class LocalProviderConnectError(ProviderCallError):
    """A local model request that failed before anything was sent."""

    loopback = False


def _connect_errors() -> tuple[type[Exception], ...]:
    from httpx import ConnectError, ConnectTimeout

    from llm_gateway.limiter import httpx as sdk_httpx

    return ConnectError, ConnectTimeout, sdk_httpx.ConnectError, sdk_httpx.ConnectTimeout


def _local_call_error(error: ProviderCallError, *, unsent: bool = False, loopback: bool = False) -> ProviderCallError:
    if error.code == "out_of_credits":
        message = "Add funds to your AI provider account, then retry."
    elif error.code == "rate_limited":
        message = f"Your AI provider is busy. Wait a moment and retry, or check the provider you selected in {_LOCAL_PROVIDER_SETTINGS}."
    elif error.code == "bad_api_key":
        message = f"Your AI provider rejected your key. Update it in {_LOCAL_PROVIDER_SETTINGS}, then retry."
    elif loopback:
        message = f"Start your model server and check Service URL in {_LOCAL_PROVIDER_SETTINGS}, then tap Try again."
    else:
        message = f"Check your model and service address in {_LOCAL_PROVIDER_SETTINGS}, then retry."
    error_type = LocalProviderConnectError if unsent else ProviderCallError
    failure = error_type(error.code, message, status=error.status, retryable=error.retryable)
    if isinstance(failure, LocalProviderConnectError):
        failure.loopback = loopback
    return failure


def _require_platform_server() -> None:
    if local_mode_enabled():
        raise ProviderCallError(
            "model_not_configured",
            f"Open {_LOCAL_PROVIDER_SETTINGS}, save your own provider and key, then retry.",
        )
    from llm_gateway.hosted import HostedAuthRequired, is_packaged_desktop

    if is_packaged_desktop():
        raise HostedAuthRequired(
            "The Mac app uses the authenticated Supymem gateway. Platform inference keys belong on the hosted backend."
        )


def _bounded_structured_mode(config: ProviderConfig, model: str, purpose: str,
                           response_schema: dict | None, tools: list | None) -> bool:
    # Clarification, evidence review, planning and content authoring need final JSON within their
    # quoted token ceiling. This is an explicit purpose policy, not a global
    # change to the model's reasoning behavior.
    # DeepSeek's thinking toggle is separate from reasoning_effort; omission
    # leaves thinking enabled. Do not alter coding/tool or other-model calls.
    return (purpose in {"idea.clarify_turn", "idea.validate", "research.deep", "plan.compile", "content.topics", "content.writer", "content.critic", "gtm.god_diagnose", "gtm.god_research", "gtm.discovery.plan", "gtm.discovery.judge", "gtm.discovery.draft", "simulation.rehearse"}
            and bool(response_schema) and not tools
            and config.provider in {"azure_foundry", "deepseek_direct"}
            and model.lower().replace("_", "-").startswith("deepseek-v4"))


def _chat_fast_mode(config: ProviderConfig, model: str, purpose: str, tools: list | None) -> bool:
    """Interactive venture chat: disable DeepSeek thinking so p95 stays interactive."""
    return (
        (purpose in {"venture.chat.assistant", "venture.chat.route", "firstlook.copy"}
         or purpose.startswith(("pagebuilder.plan_repair_", "pagebuilder.repair_")))
        and not tools
        and config.provider in {"azure_foundry", "deepseek_direct"}
        and model.lower().replace("_", "-").startswith("deepseek-v4")
    )


async def byok_chat_completion(
    messages: list[dict[str, Any]],
    *,
    config: ProviderConfig,
    tools: list[dict[str, Any]] | None = None,
    tier: str = "pro",
    max_tokens: int | None = None,
    workspace_id: str | None = None,
    purpose: str = "byok_chat_completion",
    response_schema: dict[str, Any] | None = None,
    single_attempt: bool = False,
    parallel_tool_calls: bool | None = None,
) -> Any:
    """Chat completion using an explicit founder/workspace ProviderConfig."""
    from llm_gateway.sole import APIConnectionError, APIStatusError, RateLimitError

    connect_errors = _connect_errors()
    _validate_parallel_tool_calls(parallel_tool_calls)
    if sample_mode_enabled():
        resolve_tier(purpose)
        return _sample_completion(purpose)
    captured = resolve_purpose_config(purpose, config, tier=tier)
    client, cfg = create_async_client_from_config(captured)
    # SDK option copies share this transport; close its original owner on this loop.
    owned_client = client
    try:
        if single_attempt:
            client = client.with_options(max_retries=0)
        cfg = resolve_purpose_config(purpose, cfg, tier=tier)
        model = deployment_for(cfg, tier)
        kwargs = _completion_kwargs(messages, config=cfg, model=model, purpose=purpose,
            tools=tools, max_tokens=max_tokens, response_schema=response_schema,
            parallel_tool_calls=parallel_tool_calls)
        try:
            resp = await _dial_chat(client, cfg, kwargs)
        except ProviderCallError as exc:
            if local_mode_enabled() and not loopback_provider(cfg):
                raise _local_call_error(exc) from None
            raise
        except (APIStatusError, RateLimitError, APIConnectionError, TimeoutError, *connect_errors) as exc:
            if local_mode_enabled():
                cause = exc.__cause__ if isinstance(exc, APIConnectionError) else exc
                connect_failed = isinstance(cause, connect_errors) and getattr(exc, "response", None) is None
                if connect_failed or isinstance(exc, APIConnectionError):
                    from core.local_diagnostics import write_error

                    write_error(cause or exc)
                if connect_failed:
                    # SDK retries could have sent an earlier attempt. Only opt in when they were disabled.
                    error = ProviderCallError("provider_down", "", retryable=True)
                    raise _local_call_error(error, unsent=single_attempt, loopback=loopback_provider(cfg)) from None
                raise _local_call_error(classify_provider_error(exc)) from None
            if isinstance(exc, connect_errors):
                raise
            raise classify_provider_error(exc) from exc
        note_usage(
            resp,
            workspace_id=workspace_id or get_active_workspace_id(),
            model=model,
            purpose=purpose or "byok_chat_completion",
            source="byok",
        )
        return resp
    finally:
        await _close_owned_client(owned_client)


async def env_chat_completion(
    messages: list[dict[str, Any]], *, tools: list[dict[str, Any]] | None = None,
    tier: str = "pro", max_tokens: int | None = None,
    config: ProviderConfig | None = None, workspace_id: str | None = None,
    purpose: str = "env_chat_completion",
) -> Any:
    """Legacy facade; product callers supply their registered purpose."""
    return await transmit(messages, purpose=purpose, tools=tools, tier=tier,
        max_tokens=max_tokens, config=config, workspace_id=workspace_id, single_attempt=True)


def _should_fallback(exc: ProviderCallError) -> bool:
    if not fallback_needed():
        return False
    if exc.code == "rate_limited":
        return True
    if exc.status is not None and exc.status >= 500:
        return True
    return bool(exc.retryable and exc.status in {429, 500, 502, 503, 504})


def hosted_chat_retry_config(config: ProviderConfig) -> ProviderConfig:
    if config.source != "platform_default" or config.routing_purpose != "venture.chat.assistant":
        raise ValueError("hosted_chat_retry_requires_platform_reply")
    model = (os.getenv("LLM_DEPLOYMENT_FLASH") or os.getenv("LLM_DEPLOYMENT_NAME")
        or config.deployment_flash).strip()
    captured = config.model_copy(deep=True, update={"deployment_pro": model,
        "deployment_flash": model, "captured_tier": "flash", "catalog_model": None,
        "api_mode": None, "timeout_seconds": min(config.timeout_seconds or 120, 60) / 2})
    return canonicalize_model_config(captured, model)


def fallback_needed() -> bool:
    if local_mode_enabled():
        return False
    from llm_gateway.provider_bedrock import fallback_enabled

    return fallback_enabled()


def _mark_provider(resp: Any, provider_used: str) -> Any:
    if isinstance(resp, dict):
        resp["provider_used"] = provider_used
        return resp
    try:
        resp.provider_used = provider_used
    except Exception as exc:
        logger.exception("provider_metadata_unavailable", exc_info=(RuntimeError, RuntimeError(type(exc).__name__), None))
    logger.info("provider_used", extra={"provider_used": provider_used})
    return resp


async def _foundry_chat(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None,
    tier: str,
    max_tokens: int | None,
    purpose: str,
    workspace_id: str | None,
    passthrough_model: str | None = None,
    response_schema: dict[str, Any] | None = None,
    single_attempt: bool = False,
    parallel_tool_calls: bool | None = None,
    captured_config: ProviderConfig | None = None,
) -> Any:
    from llm_gateway.sole import APIConnectionError, APIStatusError, RateLimitError

    _validate_parallel_tool_calls(parallel_tool_calls)
    if _primary_chat is not None:
        return await _primary_chat(
            messages,
            tools=tools,
            tier=tier,
            max_tokens=max_tokens,
            purpose=purpose,
            workspace_id=workspace_id,
            **({"parallel_tool_calls": parallel_tool_calls} if parallel_tool_calls is not None else {}),
            **({"captured_config": captured_config} if captured_config is not None else {}),
        )
    if captured_config is None:
        original_client, env_cfg = open_env_client(None)
        cfg = resolve_purpose_config(purpose, env_cfg, tier=tier)
        if cfg.api_mode == "responses" or cfg.deployment_pro != env_cfg.deployment_pro or cfg.deployment_flash != env_cfg.deployment_flash:
            await _close_owned_client(original_client)
            client, cfg = create_async_client_from_config(cfg)
        else:
            client = original_client
    else:
        # Already authorized, priced route. Preserve endpoint/model/credentials
        # through the dial; never re-resolve an ambient workspace override.
        _require_platform_server()
        require_env_api_key(captured_config)
        client, cfg = create_async_client_from_config(captured_config)
    # SDK option copies share this transport; close its original owner on this loop.
    owned_client = client
    try:
        if single_attempt:
            client = client.with_options(max_retries=0)
        model = resolve_live_model(cfg, tier=tier, passthrough_model=passthrough_model)
        kwargs = _completion_kwargs(messages, config=cfg, model=model, purpose=purpose,
            tools=tools, max_tokens=max_tokens, response_schema=response_schema,
            parallel_tool_calls=parallel_tool_calls)
        try:
            resp = await _dial_chat(client, cfg, kwargs)
        except (APIStatusError, RateLimitError, APIConnectionError, TimeoutError) as exc:
            raise classify_provider_error(exc) from exc
        _note_usage(resp, workspace_id=workspace_id, model=model, purpose=purpose)
        return _mark_provider(resp, "foundry")
    finally:
        await _close_owned_client(owned_client)


async def _bedrock_fallback(
    messages: list[dict[str, Any]],
    *,
    tier: str,
    max_tokens: int | None,
    purpose: str,
    workspace_id: str | None,
    single_attempt: bool = False,
) -> Any:
    if local_mode_enabled():
        _require_platform_server()
    from llm_gateway.provider_bedrock import (
        bedrock_chat,
        load_bedrock_config_from_env,
        model_for,
    )

    model = None
    started = time.monotonic()
    try:
        if _fallback_chat is not None:
            resp = await _fallback_chat(
                messages, tier=tier, max_tokens=max_tokens, purpose=purpose
            )
            return _mark_provider(resp, "bedrock")
        resp = await bedrock_chat(messages, tier=tier, max_tokens=max_tokens,
            **({"single_attempt": True} if single_attempt else {}))
        model = getattr(resp, "model", "bedrock")
        _note_usage(resp, workspace_id=workspace_id, model=str(model), purpose=purpose)
        return _mark_provider(resp, "bedrock")
    finally:
        if purpose in {"venture.chat.assistant", "venture.chat.route"} and model is None:
            try:
                model = model_for(load_bedrock_config_from_env(), tier)
            except Exception:  # noqa: BLE001 - telemetry label only
                model = "unknown"
        _log_chat_call(purpose=purpose, model=model, provider="bedrock", prompt=messages, started=started)


async def _with_fallback(
    messages: list[dict[str, Any]],
    *,
    tools: list[dict[str, Any]] | None,
    tier: str,
    max_tokens: int | None,
    purpose: str,
    workspace_id: str | None,
    passthrough_model: str | None = None,
    response_schema: dict[str, Any] | None = None,
    parallel_tool_calls: bool | None = None,
) -> Any:
    cfg = resolve_purpose_config(purpose, load_provider_config_from_env(), tier=tier)
    if cfg.api_mode == "responses":
        return await _foundry_chat(messages, tools=tools, tier=tier,
            max_tokens=max_tokens, purpose=purpose, workspace_id=workspace_id,
            passthrough_model=passthrough_model, response_schema=response_schema,
            parallel_tool_calls=parallel_tool_calls, single_attempt=True, captured_config=cfg)
    async def _call() -> Any:
        return await _foundry_chat(
            messages,
            tools=tools,
            tier=tier,
            max_tokens=max_tokens,
            purpose=purpose,
            workspace_id=workspace_id,
            passthrough_model=passthrough_model,
            response_schema=response_schema,
            parallel_tool_calls=parallel_tool_calls,
        )

    try:
        return await reliability_for(tier).run(_call)
    except ProviderCallError as exc:
        # The alternate adapter has no equivalent tool-call policy contract.
        # Never silently drop an explicitly requested tool option on fallback.
        if parallel_tool_calls is None and _should_fallback(exc):
            logger.warning(
                "foundry_exhausted_falling_back",
                extra={"code": exc.code, "status": exc.status},
            )
            return await _bedrock_fallback(
                messages,
                tier=tier,
                max_tokens=max_tokens,
                purpose=purpose,
                workspace_id=workspace_id,
            )
        raise


async def embed(
    texts: list[str],
    *,
    purpose: str = "memory.embed",
    workspace_id: str | None = None,
    config: ProviderConfig | None = None,
) -> Any:
    """Embeddings via LLM_DEPLOYMENT_EMBED. The only embed door."""
    from llm_gateway.sole import APIConnectionError, APIStatusError, RateLimitError

    wid = workspace_id or get_active_workspace_id()
    override = resolve_override(wid, config=config)
    cfg = override or load_provider_config_from_env()
    model = deployment_for(cfg, "embed")
    if override is not None:
        client, cfg = create_async_client_from_config(override)
    else:
        client, cfg = open_env_client(None)
    # SDK option copies share this transport; close its original owner on this loop.
    owned_client = client
    try:
        model = deployment_for(cfg, "embed")
        try:
            resp = await client.embeddings.create(model=model, input=texts)
        except (APIStatusError, RateLimitError, APIConnectionError, TimeoutError) as exc:
            raise classify_provider_error(exc) from exc
        _note_usage(resp, workspace_id=wid, model=model, purpose=purpose)
        return _mark_provider(resp, "foundry")
    finally:
        await _close_owned_client(owned_client)


def _validate_parallel_tool_calls(value: bool | None) -> None:
    if value is not None and type(value) is not bool:
        raise ValueError("parallel_tool_calls must be a boolean or None")


def _time_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    # Keep the stable system prefix and the final founder/tool message intact.
    # The boundary scan already qualified caller messages; this line is trusted.
    import json

    clock = time_context()
    if type(clock) is not str or len(json.dumps(clock, ensure_ascii=False)[1:-1].encode("utf-8")) > 256:
        raise ValueError("Server clock exceeds its captured allowance")
    index = next((index for index, message in enumerate(messages) if message.get("role") != "system"), len(messages))
    return [*messages[:index], {"role": "system", "content": clock}, *messages[index:]]


async def transmit(
    messages: list[dict[str, Any]],
    *,
    purpose: str,
    workspace_id: str | None = None,
    segments: Sequence[Any] | None = None,
    provider: str | None = None,
    trace_id: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    tier: str = "pro",
    max_tokens: int | None = None,
    config: ProviderConfig | None = None,
    passthrough_model: str | None = None,
    response_schema: dict[str, Any] | None = None,
    single_attempt: bool = False,
    parallel_tool_calls: bool | None = None,
    use_workspace_override: bool = True,
    hosted_chat_fallback: bool = False,
) -> Any:
    """The only door to a provider for boundary-scanned traffic (43 §4).

    parallel_tool_calls is a provider preference. Callers that require serial
    tool execution must validate the returned calls; providers may ignore it.
    Omission preserves existing provider defaults. Captured routes require a
    single attempt and bypass ambient workspace/env routing automatically.
    Legacy priced operations may explicitly set use_workspace_override=False.
    """
    _validate_parallel_tool_calls(parallel_tool_calls)
    if local_mode_enabled() and config is not None and config.source != "founder_override" and config.routing_purpose is not None:
        _require_platform_server()
    if hosted_chat_fallback and (purpose != "venture.chat.assistant" or not single_attempt
            or config is None or config.source != "platform_default"
            or provider != config.provider and not (provider == "bedrock" and fallback_needed())
            or tools or response_schema or parallel_tool_calls is not None):
        raise ValueError("hosted_chat_fallback_requires_metered_platform_reply")
    if config is not None and config.routing_purpose is not None:
        if not single_attempt:
            raise ValueError("pinned_provider_requires_explicit_single_attempt")
        use_workspace_override = False
        tier = config.captured_tier
    if not use_workspace_override:
        if config is None or config.source not in {"platform_default", "founder_override"} or not single_attempt:
            raise ValueError("pinned_provider_requires_explicit_single_attempt")
        if config.captured_tier is None and tier != "pro":
            raise ValueError("pinned_provider_requires_captured_tier")
        if config.routing_purpose is None:
            # Legacy reviewed quotes already own their route. A new operator map
            # must never change one after its admission.
            resolve_tier(purpose)
            model = deployment_for(config, tier)
            config = config.model_copy(deep=True, update={"routing_purpose": purpose,
                "captured_tier": tier})
            canonicalize_model_config(config, model)
        else:
            config = resolve_purpose_config(purpose, config, tier=tier)
        wire_api_mode(config)
    resolved = _resolved_tier(purpose, tier) if use_workspace_override else tier
    wid = workspace_id or get_active_workspace_id()
    decision = boundary_decision(
        messages,
        purpose=purpose,
        workspace_id=wid,
        segments=segments,
        provider=provider or (provider_label(resolved, config=config) if config is not None else None),
        trace_id=trace_id,
        tier=resolved,
    )

    if sample_mode_enabled() and resolved != "embed":
        return _sample_completion(purpose)
    if resolved == "local":
        return _local_completion(purpose=purpose)

    if resolved == "embed":
        texts = [
            str(m.get("content") or "")
            for m in decision.messages
            if m.get("role") == "user"
        ]
        return await embed(texts or [""], purpose=purpose, workspace_id=wid, config=config)

    # Inject once after the scan; all retries/fallbacks reuse this preamble.
    messages = _time_messages(decision.messages)
    override = resolve_override(wid, config=config) if use_workspace_override or local_mode_enabled() else (config if config.source == "founder_override" else None)
    if hosted_chat_fallback and override is not None:
        raise ValueError("hosted_chat_fallback_refuses_workspace_key")
    if override is not None:
        resp = await byok_chat_completion(
            messages,
            config=override,
            tools=tools,
            tier="pro" if resolved == "passthrough" else resolved,
            max_tokens=max_tokens,
            workspace_id=wid,
            purpose=purpose,
            response_schema=response_schema,
            single_attempt=single_attempt,
            parallel_tool_calls=parallel_tool_calls,
        )
        return _mark_provider(resp, "byok")

    _require_platform_server()
    if hosted_chat_fallback and provider == "bedrock":
        return await _bedrock_fallback(messages, tier="flash", max_tokens=max_tokens,
            purpose=purpose, workspace_id=wid, single_attempt=True)
    if single_attempt:
        # Durable metered operations own retries. A timeout is an ambiguous paid
        # request and must not cause another dial or a different provider.
        return await _foundry_chat(
            messages, tools=tools,
            tier="flash" if resolved == "passthrough" else resolved,
            max_tokens=max_tokens, purpose=purpose, workspace_id=wid,
            passthrough_model=passthrough_model if resolved == "passthrough" else None,
            response_schema=response_schema, single_attempt=True,
            parallel_tool_calls=parallel_tool_calls,
            **({"captured_config": config} if not use_workspace_override else {}),
        )
    return await _with_fallback(
        messages,
        tools=tools,
        tier="flash" if resolved == "passthrough" else resolved,
        max_tokens=max_tokens,
        purpose=purpose,
        workspace_id=wid,
        passthrough_model=passthrough_model if resolved == "passthrough" else None,
        response_schema=response_schema,
        parallel_tool_calls=parallel_tool_calls,
    )


async def transmit_stream(
    messages: list[dict[str, Any]],
    *,
    purpose: str,
    workspace_id: str | None = None,
    segments: Sequence[Any] | None = None,
    provider: str | None = None,
    trace_id: str | None = None,
    tools: list[dict[str, Any]] | None = None,
    tier: str = "pro",
    max_tokens: int | None = None,
    config: ProviderConfig | None = None,
    passthrough_model: str | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield `{type: delta|usage, ...}` chunks. Usage is recorded on the final chunk."""
    from llm_gateway.sole import APIConnectionError, APIStatusError, RateLimitError

    resolved = _resolved_tier(purpose, tier)
    wid = workspace_id or get_active_workspace_id()
    decision = boundary_decision(
        messages,
        purpose=purpose,
        workspace_id=wid,
        segments=segments,
        provider=provider,
        trace_id=trace_id,
        tier=resolved,
    )
    if sample_mode_enabled() and resolved != "embed":
        response = _sample_completion(purpose)
        yield {"type": "delta", "content": response.choices[0].message.content, "provider_used": "sample"}
        yield {"type": "usage", "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}, "provider_used": "sample"}
        return
    if resolved == "local":
        yield {"type": "delta", "content": "", "provider_used": "local"}
        yield {
            "type": "usage",
            "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
            "provider_used": "local",
        }
        return
    if resolved == "embed":
        raise ProviderCallError(
            "provider_error",
            "embeddings are not streamed; call embed() or transmit(purpose='memory.embed').",
        )

    from llm_gateway.messages import normalize_chat_messages, sanitize_chat_tools

    messages = _time_messages(decision.messages)
    client = None
    cfg = None
    try:
        override = resolve_override(wid, config=config) if config is None or config.routing_purpose is None else (config if config.source == "founder_override" else None)
        if config is not None and config.routing_purpose is not None:
            cfg = resolve_purpose_config(purpose, config, tier=config.captured_tier)
            if cfg.source != "founder_override":
                _require_platform_server()
                require_env_api_key(cfg)
            client, cfg = create_async_client_from_config(cfg)
            model = resolve_live_model(cfg, tier=cfg.captured_tier, passthrough_model=passthrough_model)
            provider_used = "byok" if cfg.source == "founder_override" else "foundry"
        elif override is not None:
            override = resolve_purpose_config(purpose, override, tier=resolved)
            client, cfg = create_async_client_from_config(override)
            model = resolve_live_model(
                cfg,
                tier=resolved if resolved != "passthrough" else "pro",
                passthrough_model=passthrough_model,
            )
            provider_used = "byok"
        else:
            client, cfg = open_env_client(None)
            model = resolve_live_model(
                cfg,
                tier=resolved if resolved != "passthrough" else "flash",
                passthrough_model=passthrough_model,
            )
            provider_used = "foundry"
            captured = resolve_purpose_config(purpose, cfg, tier=resolved)
            if captured.api_mode == "responses" or captured.deployment_pro != cfg.deployment_pro or captured.deployment_flash != cfg.deployment_flash:
                await _close_owned_client(client)
                client, cfg = create_async_client_from_config(captured)
                model = resolve_live_model(cfg, tier=resolved, passthrough_model=passthrough_model)
            else:
                cfg = captured
        if wire_api_mode(cfg, model) == "responses":
            from llm_gateway.responses import responses_stream
            kwargs = _completion_kwargs(messages, config=cfg, model=model, purpose=purpose,
                tools=tools, max_tokens=max_tokens)
            async for chunk in responses_stream(client.with_options(max_retries=0), provider_used=provider_used, **kwargs):
                receipt = chunk.pop("response", None)
                if receipt is not None:
                    _note_usage(receipt, workspace_id=wid, model=cfg.catalog_model or model,
                        purpose=purpose, source="byok" if provider_used == "byok" else "platform")
                yield chunk
            return
        loopback = loopback_provider(cfg)
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": normalize_chat_messages(messages),
            ("max_tokens" if loopback else completion_token_key(cfg.catalog_model or model)): max_tokens or cfg.max_output_tokens,
            "stream": True,
            "stream_options": {"include_usage": True},
        }
        if loopback:
            client = client.with_options(max_retries=0)
        live_tools = sanitize_chat_tools(tools)
        if live_tools:
            kwargs["tools"] = live_tools
            kwargs["tool_choice"] = "auto"
        usage_holder: dict[str, int] = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
        }
        try:
            stream = await client.chat.completions.create(**kwargs)
            async for chunk in stream:
                usage = getattr(chunk, "usage", None)
                if usage is not None:
                    counts = extract_token_usage(usage)
                    usage_holder.update(counts)
                choices = getattr(chunk, "choices", None) or []
                if choices:
                    delta = getattr(choices[0], "delta", None)
                    text = getattr(delta, "content", None) if delta is not None else None
                    if text:
                        yield {"type": "delta", "content": text, "provider_used": provider_used}
        except (APIStatusError, RateLimitError, APIConnectionError, TimeoutError) as exc:
            if local_mode_enabled():
                raise
            err = classify_provider_error(exc)
            if _should_fallback(err):
                resp = await _bedrock_fallback(
                    messages,
                    tier=resolved if resolved != "passthrough" else "flash",
                    max_tokens=max_tokens,
                    purpose=purpose,
                    workspace_id=wid,
                )
                content = ""
                choices = getattr(resp, "choices", None) or []
                if choices:
                    content = getattr(getattr(choices[0], "message", None), "content", "") or ""
                if content:
                    yield {"type": "delta", "content": content, "provider_used": "bedrock"}
                usage = extract_token_usage(getattr(resp, "usage", None))
                yield {"type": "usage", "usage": usage, "provider_used": "bedrock"}
                return
            raise err from exc
        fake = SimpleNamespace(usage=SimpleNamespace(**usage_holder))
        _note_usage(
            fake,
            workspace_id=wid,
            model=model,
            purpose=purpose,
            source="byok" if provider_used == "byok" else "platform",
        )
        yield {"type": "usage", "usage": usage_holder, "provider_used": provider_used}
    except ProviderCallError as exc:
        if local_mode_enabled() and not loopback_provider(cfg):
            raise _local_call_error(exc) from None
        raise
    except (APIStatusError, RateLimitError, APIConnectionError, TimeoutError) as exc:
        if local_mode_enabled():
            raise _local_call_error(classify_provider_error(exc)) from None
        raise
    finally:
        if client is not None:
            await _close_owned_client(client)


def boundary_decision(
    messages: list[dict[str, Any]],
    *,
    purpose: str,
    workspace_id: str | None = None,
    segments: Sequence[Any] | None = None,
    provider: str | None = None,
    trace_id: str | None = None,
    tier: str = "pro",
) -> Decision:
    """Run the boundary filter without sending. Raises `BoundaryViolation`."""
    boundary_filter = resolve_filter()
    return boundary_filter(
        messages,
        purpose=purpose,
        workspace_id=workspace_id,
        provider=provider or provider_label(tier),
        segments=segments,
        trace_id=trace_id,
    )


__all__ = [
    "BoundaryViolation",
    "Decision",
    "LocalProviderConnectError",
    "ProviderCallError",
    "ProviderConfig",
    "SecretStr",
    "boundary_decision",
    "byok_chat_completion",
    "create_async_client",
    "create_async_client_from_config",
    "deployment_for",
    "embed",
    "env_chat_completion",
    "get_active_workspace_id",
    "load_provider_config_from_env",
    "note_usage",
    "open_env_client",
    "provider_label",
    "redacted_status",
    "require_env_api_key",
    "resolve_override",
    "set_reliability_for",
    "set_transmit_hooks",
    "set_usage_recorder",
    "set_workspace_llm_resolver",
    "transmit",
    "transmit_stream",
    "workspace_llm_context",
]
