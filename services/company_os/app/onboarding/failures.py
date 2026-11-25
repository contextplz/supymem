"""Failure taxonomy and founder-facing phrasing (26 §10)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ErrorClass:
    name: str
    retryable: bool
    narration_key: str  # failures: key in narration.yaml


WARDEN_DENIED = ErrorClass("warden_denied", False, "warden_denied")
WARDEN_UNAVAILABLE = ErrorClass("warden_unavailable", True, "guardrails_offline")
LLM_UNAVAILABLE = ErrorClass("llm_unavailable", True, "thinking_unavailable")
LLM_SCHEMA = ErrorClass("llm_schema", True, "retrying_understanding")
BUDGET_EXHAUSTED = ErrorClass("budget_exhausted", False, "budget_paused")
PROVIDER_AUTH = ErrorClass("provider_auth", False, "needs_credential")
PROVIDER_RATE_LIMIT = ErrorClass("provider_rate_limit", True, "provider_busy")
PROVIDER_CREDITS = ErrorClass("provider_credits", False, "out_of_credits")
# The founder's own key ran dry (PROVIDER_CREDITS) is a different failure from
# this venture exhausting its platform allowance (doc 99 §5).
VENTURE_CREDITS = ErrorClass("venture_credits", False, "venture_credits_exhausted")
PROVIDER_DOWN = ErrorClass("provider_down", True, "thinking_unavailable")
NETWORK = ErrorClass("network", True, "network")
CODING_AGENT_NO_OP = ErrorClass("coding_agent_no_op", True, "build_wrote_nothing")
GATE_FAILED = ErrorClass("gate_failed", False, "quality_check")
INTERRUPTED = ErrorClass("interrupted", True, "interrupted")
INTERNAL = ErrorClass("internal", False, "internal")

ALL_ERROR_CLASSES: dict[str, ErrorClass] = {
    e.name: e
    for e in (
        WARDEN_DENIED,
        WARDEN_UNAVAILABLE,
        LLM_UNAVAILABLE,
        LLM_SCHEMA,
        BUDGET_EXHAUSTED,
        PROVIDER_AUTH,
        PROVIDER_RATE_LIMIT,
        PROVIDER_CREDITS,
        VENTURE_CREDITS,
        PROVIDER_DOWN,
        NETWORK,
        CODING_AGENT_NO_OP,
        GATE_FAILED,
        INTERRUPTED,
        INTERNAL,
    )
}


class StepFailed(Exception):
    def __init__(self, step_id: str, error_class: ErrorClass, detail: str = ""):
        self.step_id = step_id
        self.error_class = error_class
        self.detail = detail
        super().__init__(f"{step_id}:{error_class.name}:{detail}")


class StepError(Exception):
    """Raised inside a step to signal a classified failure."""

    def __init__(self, error_class: str, detail: str = ""):
        self.error_class = error_class
        self.detail = detail
        super().__init__(detail or error_class)


def classify(exc: BaseException) -> ErrorClass:
    if isinstance(exc, StepError):
        return ALL_ERROR_CLASSES.get(exc.error_class, INTERNAL)
    # Matched on the code attribute rather than the class so this module stays
    # free of an import from app.credits.
    if getattr(exc, "code", None) == "insufficient_credits":
        return VENTURE_CREDITS
    # Founder BYOK / platform provider taxonomy from llm_gateway.
    try:
        from llm_gateway.errors import ProviderCallError

        if isinstance(exc, ProviderCallError):
            return {
                "bad_api_key": PROVIDER_AUTH,
                "rate_limited": PROVIDER_RATE_LIMIT,
                "out_of_credits": PROVIDER_CREDITS,
                "provider_down": PROVIDER_DOWN,
            }.get(exc.code, LLM_UNAVAILABLE)
    except Exception:
        pass
    if isinstance(exc, TimeoutError):
        return NETWORK
    if isinstance(exc, (SystemExit, KeyboardInterrupt)):
        return INTERRUPTED
    name = type(exc).__name__.lower()
    msg = str(exc).lower()
    if "warden" in msg and "den" in msg:
        return WARDEN_DENIED
    if "budget" in msg:
        return BUDGET_EXHAUSTED
    if "rate" in msg and "limit" in msg:
        return PROVIDER_RATE_LIMIT
    if "auth" in msg or "credential" in msg or "bad_api_key" in msg or "api key" in msg:
        return PROVIDER_AUTH
    if "out of credits" in msg or "quota" in msg:
        return PROVIDER_CREDITS
    if "timeout" in name or "timeout" in msg or "network" in msg:
        return NETWORK
    return INTERNAL
