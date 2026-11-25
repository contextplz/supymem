"""Every purpose in docs/supymem/08 §2 must resolve; ops.* uses prefix fallback."""

from __future__ import annotations

from llm_gateway.routing import UnroutedPurposeError, concurrency_for, resolve_tier

REQUIRED = {
    "clarify.plan_turn": "pro",
    "clarify.summarize_revision": "flash",
    "research.classify": "flash",
    "research.evaluate_license": "flash",
    "research.synthesize": "pro",
    "scope.classify": "pro",
    "plan.compile": "pro",
    "estimate.explain": "flash",
    "build.qa_triage": "pro",
    "debug.diagnose": "pro",
    "iterate.interpret": "pro",
    "import.analyze": "pro",
    "app.proxy": "passthrough",
    "guard.injection": "local",
    "memory.embed": "embed",
}


def test_required_purposes_resolve() -> None:
    for purpose, tier in REQUIRED.items():
        assert resolve_tier(purpose) == tier, purpose


def test_ops_prefix_falls_back_to_flash() -> None:
    assert resolve_tier("ops.brain_cycle") == "flash"
    assert resolve_tier("ops.anything_new") == "flash"


def test_unknown_family_is_hard_error() -> None:
    try:
        resolve_tier("notafamily.nope")
        assert False, "expected UnroutedPurposeError"
    except UnroutedPurposeError:
        pass


def test_concurrency_caps() -> None:
    assert concurrency_for("pro") == 8
    assert concurrency_for("flash") == 32
