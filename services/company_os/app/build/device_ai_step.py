"""Metered model steps for the AI-primary Mac loop (SUPYMEM_CU_ENGINE=ai_loop).

Three kinds of call, all admitted against the reviewed order's credit ceiling before any provider I/O:
  step    OpenAI vision (existing OPENAI_DEVICE_VISION_API_KEY / gpt-5.6-luna): next 1-4 actions.
  plan    DeepSeek V4 Pro (platform LLM), text only: sub-goals from the reviewed task text (also re-plans).
  verify  DeepSeek V4 Pro, text only: does the final observation prove the task is done?
The goal always comes from the reviewed order, never from the request. A reply grants no authority by itself:
the sidecar still applies its hard safety blocks, focus guard and step caps.
"""
from __future__ import annotations

import base64
import asyncio
import hashlib
import json
import os
import re

import httpx
from coding_agents import cu_ai_loop as loop

from app.build import device_invocations as calls
from app.build import device_orders as orders
from app.credits.estimate import actual_llm_micro, estimate_llm_micro

MODEL = "gpt-5.6-luna"   # default answer/form/download step model
FALLBACK_MODEL = "gpt-6.1-sol"   # default model when Jev abstains (lowest wrong-target in the 2026-10-04 A/B)
ALLOWED_MODELS = frozenset({"gpt-5.6-luna", "gpt-6-luna", "gpt-6.1-sol", "gpt-6-sol"})


def _env_model(name, default):
    value = os.environ.get(name, "").strip()
    return value if value in ALLOWED_MODELS else default


def step_model():
    return _env_model("SUPYMEM_CU_STEP_MODEL", MODEL)


def fallback_model():
    return _env_model("SUPYMEM_CU_FALLBACK_MODEL", FALLBACK_MODEL)
ENDPOINT = "https://api.openai.com/v1/responses"
MAX_IMAGE_BYTES = 1_500_000
MAX_OUT = 2500
# now_line() is bounded to 60 characters. Quote that allowance independently of wall time.
QUOTE_CLOCK_LINE = "NOW: " + "0" * 55
_client = httpx.Client(timeout=httpx.Timeout(40, connect=5), follow_redirects=False, trust_env=False)


def priority_tier() -> bool:
    """SUPYMEM_CU_PRIORITY_TIER=1 sends step calls with OpenAI service_tier=priority (about 0.6 s faster per step, billed at
    SUPYMEM_CU_PRIORITY_MULTIPLIER x). Env-driven; documented as on in production config."""
    return os.environ.get("SUPYMEM_CU_PRIORITY_TIER", "").strip().lower() in {"1", "true", "on", "yes"}


def _scope(venture_id, caller, order_id):
    saved = orders.retrieve(venture_id, caller, order_id)
    manifest = orders.Manifest.model_validate(saved["manifest"])
    if manifest.desktop_verification is None or saved.get("state") != "claimed":
        raise ValueError("device_ai_step_scope_invalid")
    return saved, manifest, manifest.desktop_verification.founder_goal or manifest.instruction


def _text(value, limit):
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError("device_ai_step_request_invalid")
    return value


def _list(value, limit, item_limit):
    if not isinstance(value, list) or len(value) > limit:
        raise ValueError("device_ai_step_request_invalid")
    return [_text(item, item_limit) for item in value]


def _openai(body: dict, *, config=None) -> dict:
    if config is not None and config.provider == "anthropic":
        from llm_gateway.local_transport import anthropic_vision

        return anthropic_vision(body, config)
    if config is None:
        key = os.environ.get("OPENAI_DEVICE_VISION_API_KEY", "")
        endpoint = ENDPOINT
    else:
        from llm_gateway.provider_foundry import normalize_base_url

        key = config.api_key.get_secret_value()
        endpoint = normalize_base_url(config.provider, config.base_url).rstrip("/") + "/responses"
    if not key:
        raise ValueError("device_vision_not_enabled")
    response = _client.post(endpoint, headers={"Authorization": f"Bearer {key}"}, json=body)
    if response.status_code != 200 or len(response.content) > 200_000:
        raise ValueError("device_ai_step_provider_failed")
    return response.json()


def _deepseek(system: str, user: str, max_tokens: int, *, config, workspace_id: str, kind: str) -> tuple[str, str, int, int]:
    """Platform LLM (DeepSeek V4 Pro on azure_foundry). Returns (text, model, in_tokens, out_tokens)."""
    from llm_gateway.sole import transmit

    if not config.api_key.get_secret_value():
        raise ValueError("device_ai_planner_not_enabled")
    result = asyncio.run(transmit(
        [{"role": "system", "content": system}, {"role": "user", "content": user}],
        purpose="device.ai_step." + kind, workspace_id=workspace_id,
        config=config, provider=config.provider + ":" + config.deployment_pro,
        tier="pro", max_tokens=max_tokens, single_attempt=True, use_workspace_override=False))
    usage = result.usage
    tin, tout, total = (getattr(usage, key, None) for key in ("prompt_tokens", "completion_tokens", "total_tokens"))
    if (any(type(value) is not int for value in (tin, tout, total))
            or not 0 <= tin <= 200_000 or not 0 <= tout <= max_tokens or total != tin + tout):
        raise ValueError("device_ai_step_usage_unconfirmed")
    return result.choices[0].message.content or "", config.deployment_pro, tin, tout


def _admit(venture_id, caller, order_id, invocation_id, digest_source, model, prompt_tokens, max_tokens, priority=False):
    binding = {**digest_source, "model": model, "max_tokens": max_tokens, "priority": priority}
    digest = hashlib.sha256(json.dumps(binding, sort_keys=True, default=str).encode()).hexdigest()
    quote = estimate_llm_micro(model, prompt_tokens=prompt_tokens, max_tokens=max_tokens, priority=priority)
    if not calls.admit(venture_id, caller, order_id, invocation_id=invocation_id, request_digest=digest,
                       quote_micro=quote)["admitted_now"]:
        raise ValueError("device_ai_step_already_attempted")
    return quote


def _settle(venture_id, caller, order_id, invocation_id, model, tin, tout, priority=False):
    calls.record_result(venture_id, caller, order_id, invocation_id=invocation_id,
                        actual_micro=actual_llm_micro(model, prompt_tokens=tin, completion_tokens=tout, priority=priority),
                        usage={"model": model, "input_tokens": tin, "output_tokens": tout, **({"service_tier": "priority"} if priority else {})})


def _uncertain(venture_id, caller, order_id, invocation_id):
    calls.record_result(venture_id, caller, order_id, invocation_id=invocation_id, actual_micro=None)


def run(venture_id, caller, order_id, *, invocation_id: str, kind: str, context: dict,
        image_base64: str | None = None) -> dict:
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", invocation_id or "") or not isinstance(context, dict):
        raise ValueError("device_ai_step_request_invalid")
    version = context.get("action_contract_version", 1)
    if type(version) is not int or version not in {1, 2, 3}:
        raise ValueError("action_contract_version_invalid")
    saved, _, goal = _scope(venture_id, caller, order_id)
    from app.core.local_mode import is_local

    config = None
    if is_local():
        from app.llm.founder_provider import local_vision_config

        config = local_vision_config(saved["workspace_id"])
    if kind == "step":
        if config is not None:
            return _step(venture_id, caller, order_id, invocation_id, goal, context, image_base64, config=config)
        return _step(venture_id, caller, order_id, invocation_id, goal, context, image_base64)
    if kind in {"plan", "verify"}:
        return _text_call(venture_id, caller, order_id, invocation_id, goal, kind, context,
                          workspace_id=saved["workspace_id"], local_config=config)
    raise ValueError("device_ai_step_request_invalid")


def _step(venture_id, caller, order_id, invocation_id, goal, ctx, image_b64, *, config=None):
    elements = ctx.get("elements")
    if not isinstance(elements, list) or len(elements) > 400 or any(not isinstance(e, dict) for e in elements):
        raise ValueError("device_ai_step_request_invalid")
    text_args = (
        goal, _list(ctx.get("subgoals", []), 12, 400), int(ctx.get("subgoal", 0)), _text(ctx.get("memory", ""), 800),
        _list(ctx.get("history", []), 12, 300), _text(ctx.get("frontmost", ""), 200),
        _text(ctx.get("window_title", ""), 300), elements, _text(ctx.get("hint", ""), 500),
        _text(ctx.get("notes", ""), 2600))
    user_text = loop.build_user_text(*text_args)
    quote_text = loop.build_user_text(*text_args, clock_line=QUOTE_CLOCK_LINE)
    image = ""
    if image_b64:
        raw = base64.b64decode(_text(image_b64, 2_100_000), validate=True)
        if not 0 < len(raw) <= MAX_IMAGE_BYTES or not raw.startswith(b"\x89PNG"):
            raise ValueError("device_vision_image_invalid")
        image = image_b64
    version = ctx.get("action_contract_version", 1)
    if type(version) is not int or version not in {1, 2, 3}:
        raise ValueError("action_contract_version_invalid")
    if config is not None:
        from llm_gateway.provider_foundry import normalize_base_url

    model = config.deployment_pro if config is not None else step_model()
    body = loop.build_request_body(model, user_text, image, MAX_OUT, action_contract_version=version)
    max_out = body["max_output_tokens"] if model in loop.NO_NONE_EFFORT else MAX_OUT
    priority = priority_tier()
    if priority:
        body["service_tier"] = "priority"
    quote = _admit(venture_id, caller, order_id, invocation_id,
           {"kind": "step", "goal": goal, "context": ctx, "image": hashlib.sha256(image.encode()).hexdigest(),
            "action_contract_version": version, "provider": config.provider if config is not None else "openai",
            "endpoint": normalize_base_url(config.provider, config.base_url) if config is not None else ENDPOINT,
            "system": body["input"][0], "format": body["text"], "reasoning": body["reasoning"]},
           model, min(len(quote_text) // 3 + 3500, 64000), max_out, priority=priority)
    try:
        if config is not None:
            from types import SimpleNamespace
            from app.credits import metered

            saved = orders.retrieve(venture_id, caller, order_id)
            value = vars(metered.call_sync(lambda: SimpleNamespace(**_openai(body, config=config)),
                purpose="device.ai_step.step", workspace_id=saved["workspace_id"], venture_id=venture_id,
                request_id=order_id + ":" + invocation_id, provider=config.provider, model=model,
                estimate_micro=quote, max_tokens=max_out, payer="founder",
                actual_micro=lambda result: actual_llm_micro(model,
                    prompt_tokens=result.usage["input_tokens"], completion_tokens=result.usage["output_tokens"],
                    priority=priority and getattr(result, "service_tier", None) == "priority")))
        else:
            value = _openai(body)
        usage = value.get("usage") or {}
        tin, tout = int(usage["input_tokens"]), int(usage["output_tokens"])
        if not str(value.get("model", "")).startswith(model) or not 0 <= tin <= 200_000 or not 0 <= tout <= max_out:
            raise ValueError("usage")
    except Exception as exc:
        from app.credits.local_meter import LocalSpendLimit

        if config is not None and isinstance(exc, LocalSpendLimit):
            calls.record_result(venture_id, caller, order_id, invocation_id=invocation_id, actual_micro=0)
        else:
            _uncertain(venture_id, caller, order_id, invocation_id)
        if config is not None:
            from app.credits.errors import CreditsUnavailable

            if isinstance(exc, CreditsUnavailable):
                raise
            raise ValueError("Your model response could not be confirmed. Open Settings > Keys and connections, check your vision model, then retry your task.") from None
        raise ValueError("device_ai_step_provider_or_usage_unconfirmed") from None
    # Bill the priority price only when OpenAI says it served the call on that tier (the quote already reserved the ceiling).
    _settle(venture_id, caller, order_id, invocation_id, model, tin, tout, priority=priority and value.get("service_tier") == "priority")
    calls.check_current(venture_id, caller, order_id)
    reply = loop.response_text(value)
    loop.parse_reply(reply, action_contract_version=version)   # reject malformed/unsupported replies here; the sidecar re-validates and applies safety blocks
    return {"authority": "proposal_only", "reply": reply, "model": model, "input_tokens": tin, "output_tokens": tout}


def _text_call(venture_id, caller, order_id, invocation_id, goal, kind, ctx, *, workspace_id, local_config=None):
    if kind == "plan":
        system = loop.PLAN_PROMPT
        user = (f"TASK: {goal[:6000]}\nREASON: {_text(ctx.get('reason', 'start'), 200)}\n"
                f"MEMORY: {_text(ctx.get('memory', ''), 800) or '-'}\n"
                f"SCREEN: {_text(ctx.get('screen', ''), 1500) or '-'}")
        quote_text = user
    else:
        system = loop.VERIFY_PROMPT
        text_args = (goal, _text(ctx.get("claim", ""), 600), _text(ctx.get("frontmost", ""), 200),
                     _text(ctx.get("window_title", ""), 300),
                     [e for e in ctx.get("elements", []) if isinstance(e, dict)][:300],
                     _text(ctx.get("memory", ""), 5000), _text(ctx.get("notes", ""), 2600))
        user = loop.verify_text(*text_args)
        quote_text = loop.verify_text(*text_args, clock_line=QUOTE_CLOCK_LINE)
    used = "deepseek"
    try:
        from llm_gateway.provider_foundry import load_provider_config_from_env
        config = (local_config if local_config is not None else load_provider_config_from_env()).model_copy(deep=True,
            update={"timeout_seconds": 30, "reasoning_effort": ""})
        model = config.deployment_pro
        route = config.model_dump(include={"provider", "base_url", "deployment_pro", "reasoning_effort",
                                           "timeout_seconds", "max_output_tokens", "source"})
    except Exception:
        if local_config is not None:
            raise
        model, used = step_model(), "openai"
        route = {"provider": "openai", "endpoint": ENDPOINT, "reasoning": loop.reasoning_for(model, 700)[0]}
    max_tokens = 700
    if used == "deepseek":
        from llm_gateway.routing import resolve_purpose_config, sol_output_cap
        config = resolve_purpose_config("device.ai_step." + kind, config, tier="pro")
        model = config.deployment_pro
        max_tokens = sol_output_cap(config, max_tokens)
        route.update(model=model, max_output_tokens=max_tokens)
    _admit(venture_id, caller, order_id, invocation_id,
           {"kind": kind, "system": system, "goal": goal, "context": ctx, "route": route},
           model, len(quote_text) // 3 + 800, loop.reasoning_for(model, 700)[1] if used == "openai" else max_tokens)
    try:
        if used == "deepseek":
            text, model, tin, tout = _deepseek(system, user, max_tokens, config=config,
                                            workspace_id=workspace_id, kind=kind)
        else:
            body = loop.build_text_request_body(model, system, user, kind, 700)
            value = _openai(body)
            usage = value.get("usage") or {}
            tin, tout = int(usage["input_tokens"]), int(usage["output_tokens"])
    except Exception:
        # An admitted request may already have reached its provider. Preserve
        # that ceiling until reconciliation; never replay through another one.
        # OpenAI is selected only before admission when platform config is absent.
        _uncertain(venture_id, caller, order_id, invocation_id)
        raise ValueError("device_ai_step_provider_or_usage_unconfirmed") from None
    _settle(venture_id, caller, order_id, invocation_id, model, tin, tout)
    calls.check_current(venture_id, caller, order_id)
    if used == "openai":
        text = loop.response_text(value)
    return {"authority": "proposal_only", "text": text[:4000], "model": model, "input_tokens": tin,
            "output_tokens": tout}
