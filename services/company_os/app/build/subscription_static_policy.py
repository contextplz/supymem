"""QA-only policy for reviewed subscription static source; no builder/model path."""
from __future__ import annotations

from decimal import Decimal, ROUND_CEILING

from app.builder.budgets import DEADLINES, _object
from app.builder.lifecycle import INCLUDED, validate_rate
from app.builder.model_proxy import BuildModelError, digest
from app.infra.fargate_qa import QaProfile
from app.infra.fargate_build import BuildProviderError

POLICY_ID = "subscription_static_source_qa_v1"
DEADLINE_KEYS = {"queue_seconds", "qa_seconds", "qa_stop_seconds", "margin_seconds"}


def validate(runtime: dict) -> dict:
    """Only independently priced QA compute may be reserved or charged."""
    try:
        profile = QaProfile.from_dict(runtime["qa_profile"]).snapshot()
        rate = runtime["task_pricing"]["qa"]
        validate_rate(rate, profile)
        bounds = runtime["deadlines"]
        if (runtime.get("lifecycle_version") != 3 or runtime.get("policy_id") != POLICY_ID
                or runtime.get("artifact_profile") != "static_landing_v1"
                or any(type(runtime.get(key)) is not int or runtime[key] != 0 for key in (
                    "model_ceiling_micro", "builder_compute_ceiling_micro", "artifact_ceiling_micro"))
                or set(runtime["task_pricing"]) != {"qa"}
                or any(key in runtime for key in ("model_config", "builder_profile", "fargate_profile"))
                or runtime.get("included_overhead") != INCLUDED
                or set(bounds) != DEADLINE_KEYS
                or any(type(value) is not int or not 1 <= value <= 3600 for value in bounds.values())):
            raise ValueError()
        ceiling = int((Decimal(rate["micro_per_second"]) * max(
            60, bounds["qa_seconds"] + bounds["qa_stop_seconds"])).quantize(
                Decimal(1), rounding=ROUND_CEILING))
        if (type(runtime.get("ceiling_micro")) is not int or ceiling <= 0
                or runtime["ceiling_micro"] != ceiling
                or runtime.get("compute_ceiling_micro") != ceiling
                or runtime.get("qa_compute_ceiling_micro") != ceiling):
            raise ValueError()
    except (KeyError, TypeError, ValueError, ArithmeticError, BuildModelError, BuildProviderError) as error:
        raise BuildModelError("subscription_static_qa_policy_invalid") from error
    return runtime


def configured() -> dict:
    """Read QA profile/rate only; never resolve or quote a model or builder."""
    try:
        profile = QaProfile.from_dict(_object("BUILD_QA_FARGATE_PROFILE")).snapshot()
        rate = dict(_object("BUILD_FARGATE_TASK_PRICING")["qa"])
        rate["profile_digest"] = digest(profile)
        bounds = {key: DEADLINES[key] for key in DEADLINE_KEYS}
        validate_rate(rate, profile)
        ceiling = int((Decimal(rate["micro_per_second"]) * max(
            60, bounds["qa_seconds"] + bounds["qa_stop_seconds"])).quantize(
                Decimal(1), rounding=ROUND_CEILING))
        return validate({"lifecycle_version": 3, "policy_id": POLICY_ID,
            "artifact_profile": "static_landing_v1", "ceiling_micro": ceiling,
            "model_ceiling_micro": 0, "builder_compute_ceiling_micro": 0,
            "qa_compute_ceiling_micro": ceiling, "compute_ceiling_micro": ceiling,
            "artifact_ceiling_micro": 0, "included_overhead": INCLUDED.copy(),
            "qa_profile": profile, "task_pricing": {"qa": rate}, "deadlines": bounds})
    except Exception as error:
        raise BuildModelError("subscription_static_qa_policy_unavailable") from error
