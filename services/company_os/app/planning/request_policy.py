"""One tool-free request contract for the planner quote and durable execution.

Changing reasoning does not expand its output limit or authorize a second call.
The preserved ExecutionPlan context is projected by compiler._project before
entering here; this module does not truncate or reconstruct evidence.
"""
from __future__ import annotations

import os
from dataclasses import dataclass

from app.planning import author

POLICY_ID = "plan_compile_bounded_v8"
PURPOSE = "plan.compile"
# Nine complete milestones, their task contracts and cited precedents exceeded
# 8192 tokens in production. The same ceiling feeds quote and execution; this
# does not retry a truncated call or enlarge its existing reservation.
MAX_OUTPUT_TOKENS = 16384
# A complete plan is ~30-40k characters. Production (2026-10-06) took 113-130s per call and one
# call hit the old 180s bound, so it gets 240s; one call plus one validation repair (480s) stays
# inside the 900s paid-job watchdog. Clarify has its own 180s provider deadline
# (idea.runner.CLARIFY_TIMEOUT_SECONDS); validate keeps the 90s interactive cap
# (research.metered.INTERACTIVE_TIMEOUT_SECONDS).
PROVIDER_TIMEOUT_SECONDS = int(os.getenv("LLM_PLAN_COMPILE_TIMEOUT_SECONDS", "240"))


@dataclass(frozen=True)
class Request:
    messages: list[dict]
    schema: dict
    max_output_tokens: int = MAX_OUTPUT_TOKENS
    provider_timeout_seconds: int = PROVIDER_TIMEOUT_SECONDS

    def estimate(self, model: str) -> int:
        from app.credits.estimate import count_prompt_tokens, estimate_llm_micro
        return estimate_llm_micro(model, prompt_tokens=count_prompt_tokens(self.messages),
                                  max_tokens=self.max_output_tokens)


def prepare(context: dict) -> Request:
    return Request(messages=author.messages(context), schema=author.generation_schema(context))


def public_policy() -> dict:
    return {"id": POLICY_ID, "purpose": PURPOSE, "max_output_tokens": MAX_OUTPUT_TOKENS,
            "provider_timeout_seconds": PROVIDER_TIMEOUT_SECONDS,
            "structured_output_required": True, "tool_calls_allowed": False,
            "automatic_provider_retry": False, "charge_cap": "reserved_ceiling"}
