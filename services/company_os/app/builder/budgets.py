"""Fail-closed production quote for the reviewed isolated build lifecycle.

The runtime is available only when deployment supplies two immutable Fargate
profiles and a recent price snapshot. Repository defaults therefore stay inert,
while the founder approves the exact profiles later used by the launcher.
"""
from __future__ import annotations

from decimal import Decimal, ROUND_CEILING
import json
import logging
import os

from app.llm.founder_provider import resolve_llm_config

log = logging.getLogger(__name__)

MAX_STEPS = 40
PRODUCT_MAX_STEPS = 56
MAX_PROMPT_TOKENS_PER_STEP = 131072 + (120 * 64)
MAX_OUTPUT_TOKENS_PER_STEP = 16384
# These are the only tool names registered by the pinned OpenHands builder.
# The Core model proxy enforces this reviewed allowlist before every provider
# call, so omitting it makes every real engineering request fail closed before
# the first model response.
MANAGED_TOOL_NAMES = ["terminal", "file_editor", "finish", "think"]
DEADLINES = {
    "queue_seconds": 120,
    # A real 56-step product build reached its bounded completion pass but was
    # stopped at 20 minutes before source capture. The quote and controller
    # share this bound so the extra time is reviewed and metered up front.
    "builder_seconds": 2400,
    "builder_stop_seconds": 120,
    "qa_seconds": 900,
    "qa_stop_seconds": 120,
    "margin_seconds": 120,
}


def _unavailable(resolved, *reasons, product: bool = False):
    step_limit = PRODUCT_MAX_STEPS if product else MAX_STEPS
    return {
        "ready": False,
        "reasons": list(reasons),
        "model_ceiling_micro": None,
        "compute_ceiling_micro": None,
        "artifact_ceiling_micro": None,
        "ceiling_micro": None,
        "model": resolved.model,
        "model_config": {
            "model": resolved.model,
            "provider": resolved.provider,
            "source": resolved.source,
            "pricing": model_pricing(resolved.model),
        },
        "runtime_policy": {
            "profile": "fargate_openhands_v1",
            "verified_execution": False,
            "max_steps": step_limit,
            "timeout_seconds": DEADLINES["builder_seconds"],
            "max_output_bytes": 1048576,
            "tool_names": MANAGED_TOOL_NAMES.copy(),
        },
        "pricing": None,
    }


def _object(name):
    raw = os.environ.get(name, "")
    if not raw or len(raw) > 20000:
        raise ValueError(name + " is absent or too large")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError(name + " must be an object")
    return value


def runtime_quote(workspace_id: str, *, product: bool = False) -> dict:
    step_limit = PRODUCT_MAX_STEPS if product else MAX_STEPS
    resolved = resolve_llm_config(workspace_id)
    if (resolved.provider, resolved.model, resolved.source) != (
        "azure_foundry",
        "DeepSeek-V4-Pro",
        "platform_default",
    ):
        return _unavailable(resolved, "build_model_configuration_unavailable", product=product)
    pricing = model_pricing(resolved.model)
    if not pricing:
        return _unavailable(resolved, "build_model_pricing_unverified", product=product)
    try:
        from app.builder.lifecycle import INCLUDED, validate_runtime
        from app.builder.model_proxy import digest
        from app.infra.fargate_build import FargateProfile
        from app.infra.fargate_qa import QaProfile

        builder = FargateProfile.from_dict(_object("BUILD_FARGATE_PROFILE")).snapshot()
        qa = QaProfile.from_dict(_object("BUILD_QA_FARGATE_PROFILE")).snapshot()
        rates = _object("BUILD_FARGATE_TASK_PRICING")
        if set(rates) != {"builder", "qa"}:
            raise ValueError("pricing purposes differ")
        for purpose, profile in (("builder", builder), ("qa", qa)):
            if not isinstance(rates[purpose], dict):
                raise ValueError("pricing row invalid")
            rates[purpose] = dict(rates[purpose])
            rates[purpose]["profile_digest"] = digest(profile)

        per_step = max(
            100,
            priced_usage(
                pricing,
                prompt_tokens=MAX_PROMPT_TOKENS_PER_STEP,
                completion_tokens=MAX_OUTPUT_TOKENS_PER_STEP,
            ),
        )
        model_ceiling = per_step * step_limit

        def compute_ceiling(purpose):
            seconds = DEADLINES[purpose + "_seconds"] + DEADLINES[purpose + "_stop_seconds"]
            return int(
                (Decimal(str(rates[purpose]["micro_per_second"])) * max(60, seconds)).quantize(
                    Decimal(1), rounding=ROUND_CEILING
                )
            )

        builder_ceiling = compute_ceiling("builder")
        qa_ceiling = compute_ceiling("qa")
        runtime = {
            "lifecycle_version": 2,
            "ready": True,
            "reasons": [],
            "ceiling_micro": model_ceiling + builder_ceiling + qa_ceiling,
            "model_ceiling_micro": model_ceiling,
            "compute_ceiling_micro": builder_ceiling + qa_ceiling,
            "builder_compute_ceiling_micro": builder_ceiling,
            "qa_compute_ceiling_micro": qa_ceiling,
            "artifact_ceiling_micro": 0,
            "included_overhead": INCLUDED.copy(),
            "model": resolved.model,
            "model_config": {
                "model": resolved.model,
                "provider": resolved.provider,
                "source": resolved.source,
                "pricing": pricing,
            },
            "runtime_policy": {
                "profile": "fargate_openhands_v1",
                "verified_execution": True,
                "max_steps": step_limit,
                "timeout_seconds": DEADLINES["builder_seconds"],
                "max_output_bytes": 1048576,
                "tool_names": MANAGED_TOOL_NAMES.copy(),
            },
            "fargate_profile": builder,
            "builder_profile": builder,
            "qa_profile": qa,
            "task_pricing": rates,
            "deadlines": dict(DEADLINES),
            "pricing": {"model": pricing, "builder": rates["builder"], "qa": rates["qa"]},
        }
        validate_runtime(runtime)
        return runtime
    except Exception as exc:
        log.warning(
            "reviewed build runtime configuration rejected (%s): %s",
            type(exc).__name__,
            str(exc)[:160],
        )
        return _unavailable(resolved, "build_runtime_configuration_unverified", product=product)


def model_pricing(model):
    from app.credits.estimate import _lookup_model_row, _row_rates
    from app.credits.units import MICRO_PER_USD

    row = _lookup_model_row(model)
    if not row:
        return None
    input_rate, output_rate = _row_rates(row)
    if not all(rate.is_finite() and rate >= 0 for rate in (input_rate, output_rate)):
        return None
    return {
        "model": model,
        "input_per_mtok": str(input_rate),
        "output_per_mtok": str(output_rate),
        "micro_per_usd": MICRO_PER_USD,
        "source": "configured_model_rate_table",
    }


def priced_usage(snapshot, *, prompt_tokens, completion_tokens):
    from decimal import ROUND_HALF_UP

    amount = (
        Decimal(snapshot["input_per_mtok"]) * prompt_tokens
        + Decimal(snapshot["output_per_mtok"]) * completion_tokens
    ) / Decimal(1000000)
    return int((amount * snapshot["micro_per_usd"]).quantize(Decimal(1), rounding=ROUND_HALF_UP))
