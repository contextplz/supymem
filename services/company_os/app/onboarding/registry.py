"""Step protocol and STEPS registry (26 §4)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal

from app.onboarding.state import Ctx

Phase = str

# The original idea-mode phase list. `first_look` sits before `commit` on
# purpose: the founder sees a real page before being asked anything (45 §1).
PHASES: tuple[Phase, ...] = (
    "orient",
    "understand",
    "first_look",
    "validate",
    "validation_gate",
    "commit",
    "provision",
    "build",
    "handoff",
)

# One step machine, one phase registry per entry mode (35 §6, LD-49).
PHASES_BY_MODE: dict[str, tuple[Phase, ...]] = {
    "idea": PHASES,
    "surprise": ("orient", "scan", "slate"),
    "engagement": ("orient", "triage", "plan", "work", "review", "deliver"),
    "existing_company": ("orient", "connect", "discover", "map", "propose", "prove"),
}


def phases_for_mode(entry_mode: str) -> tuple[Phase, ...]:
    return PHASES_BY_MODE.get(entry_mode, PHASES)

StepFn = Callable[[Ctx], Awaitable["StepResult"]]


@dataclass(frozen=True)
class PauseRequest:
    reason: str
    resume_token: str | None = None
    founder_action: str | None = None


@dataclass
class StepResult:
    output: dict[str, Any] = field(default_factory=dict)
    detail: list[str] | None = None
    query: str | None = None
    evidence_url: str | None = None
    undo: dict[str, str] | None = None
    simulated: bool = False
    partial: bool = False
    pause: PauseRequest | None = None
    cost_usd: float = 0.0
    tokens: dict[str, Any] | None = None
    variant_key: str | None = None
    demand: str | None = None
    downgraded: bool = False
    kind: str | None = None


@dataclass(frozen=True)
class StepDef:
    id: str
    phase: Phase
    fn: StepFn
    narrate: str
    kind: str = "step"
    capability: str | None = None
    required: bool = True
    retries: int = 0
    timeout_s: int = 60
    est_cost_usd: float = 0.0
    skip_if: Callable[[Ctx], bool] | None = None


def _lazy_steps() -> list[StepDef]:
    from app.onboarding.steps import (
        build,
        commit,
        handoff,
        orient,
        provision,
        understand,
        validate,
    )

    return [
        # Phase 0
        StepDef("orient.read_brief", "orient", orient.read_brief, "orient.read_brief", timeout_s=5),
        StepDef("orient.create_home", "orient", orient.create_home, "orient.create_home", timeout_s=5),
        StepDef("orient.save_words", "orient", orient.save_words, "orient.save_words", timeout_s=5),
        StepDef(
            "orient.check_permissions",
            "orient",
            orient.check_permissions,
            "orient.check_permissions",
            timeout_s=5,
        ),
        # Phase 1
        StepDef(
            "understand.classify",
            "understand",
            understand.classify,
            "understand.classify",
            timeout_s=10,
        ),
        StepDef(
            "understand.product_ask",
            "understand",
            understand.product_ask,
            "understand.product_ask",
            timeout_s=15,
        ),
        StepDef(
            "understand.product_brief",
            "understand",
            understand.product_brief,
            "understand.product_brief",
            timeout_s=10,
        ),
        StepDef(
            "understand.research_founder",
            "understand",
            understand.research_founder,
            "understand.search_skipped",
            kind="step",
            required=False,
            skip_if=lambda ctx: not bool(ctx.run.research_founder),
            timeout_s=15,
        ),
        StepDef(
            "understand.research_market",
            "understand",
            understand.research_market,
            "understand.research_market",
            kind="search",
            required=False,
            skip_if=lambda ctx: bool(ctx.flags().get("skip_research")),
            timeout_s=180,
        ),
        StepDef(
            "understand.score_complexity",
            "understand",
            understand.score_complexity,
            "understand.score_complexity",
            timeout_s=10,
        ),
        StepDef(
            "understand.question_budget",
            "understand",
            understand.question_budget_step,
            "understand.question_budget",
            timeout_s=5,
        ),
        # No first_look phase: template pages are retired; Claude Code / Codex builds the page.
        # Phase validate
        StepDef(
            "validate.generate_questions",
            "validate",
            validate.generate_questions,
            "validate.generate_questions",
            timeout_s=15,
        ),
        StepDef("validate.ask", "validate", validate.ask, "validate.ask", timeout_s=5),
        StepDef(
            "validate.office_hours_plan",
            "validate",
            validate.office_hours_plan,
            "validate.office_hours_plan",
            timeout_s=10,
        ),
        StepDef(
            "validate.demand_read",
            "validate",
            validate.demand_read,
            "validate.demand_read",
            kind="search",
            timeout_s=180,
        ),
        StepDef(
            "validate.recommendation",
            "validate",
            validate.recommendation,
            "validate.recommendation",
            timeout_s=5,
        ),
        # Validation is a persisted gate: it cannot silently advance to build.
        StepDef(
            "validation.review_choice",
            "validation_gate",
            validate.review_choice,
            "validation.review_choice",
            timeout_s=5,
        ),
        StepDef(
            "validation.decision",
            "validation_gate",
            validate.decision,
            "validation.decision",
            timeout_s=5,
        ),
        # Phase commit
        StepDef("commit.mission", "commit", commit.mission, "commit.mission", timeout_s=15),
        StepDef(
            "commit.lock_charter",
            "commit",
            commit.lock_charter_step,
            "commit.lock_charter",
            timeout_s=90,
        ),
        StepDef("commit.roadmap", "commit", commit.roadmap, "commit.roadmap", timeout_s=15),
        StepDef(
            "commit.file_documents",
            "commit",
            commit.file_documents,
            "commit.file_documents",
            timeout_s=10,
        ),
        StepDef(
            "commit.compose_team",
            "commit",
            commit.compose_team,
            "commit.compose_team",
            timeout_s=20,
        ),
        StepDef("commit.hire", "commit", commit.hire, "commit.hire", timeout_s=20),
        # Phase 4
        StepDef(
            "provision.workspace",
            "provision",
            provision.workspace,
            "provision.workspace",
            timeout_s=10,
        ),
        StepDef("provision.repo", "provision", provision.repo, "provision.repo", timeout_s=15),
        StepDef(
            "provision.database",
            "provision",
            provision.database,
            "provision.database",
            required=False,
            timeout_s=15,
        ),
        StepDef(
            "provision.email",
            "provision",
            provision.email,
            "provision.email",
            required=False,
            timeout_s=10,
        ),
        StepDef(
            "provision.hosting",
            "provision",
            provision.hosting,
            "provision.hosting",
            required=False,
            timeout_s=15,
        ),
        StepDef(
            "provision.sandbox",
            "provision",
            provision.sandbox,
            "provision.sandbox",
            timeout_s=10,
        ),
        # Phase 5
        StepDef("build.draft_adr", "build", build.draft_adr, "build.draft_adr", timeout_s=15),
        StepDef(
            "build.plan_work_orders",
            "build",
            build.plan_work_orders,
            "build.plan_work_orders",
            timeout_s=15,
        ),
        StepDef(
            "build.landing_page",
            "build",
            build.landing_page,
            "build.landing_page",
            timeout_s=60,
        ),
        StepDef("build.run_gates", "build", build.run_gates, "build.run_gates", timeout_s=20),
        StepDef("build.publish", "build", build.publish, "build.publish", timeout_s=20),
        StepDef(
            "build.close_sandbox",
            "build",
            build.close_sandbox,
            "build.close_sandbox",
            timeout_s=10,
        ),
        # Phase 6
        StepDef(
            "handoff.human_tasks",
            "handoff",
            handoff.human_tasks,
            "handoff.human_tasks",
            timeout_s=15,
        ),
        StepDef(
            "handoff.plan_next_tasks",
            "handoff",
            handoff.plan_next_tasks,
            "handoff.plan_next_tasks",
            timeout_s=20,
        ),
        StepDef(
            "handoff.queue_tasks",
            "handoff",
            handoff.queue_tasks,
            "handoff.queue_tasks",
            timeout_s=10,
        ),
        StepDef("handoff.recap", "handoff", handoff.recap, "handoff.recap", timeout_s=10),
        StepDef(
            "handoff.welcome_email",
            "handoff",
            handoff.welcome_email,
            "handoff.welcome_email",
            timeout_s=10,
        ),
        StepDef("handoff.done", "handoff", handoff.done, "handoff.done", timeout_s=10),
    ]


def _mode_steps(mode: str) -> list[StepDef]:
    """Per-mode step lists. Each mode reuses the same orchestrator (LD-49)."""
    if mode == "engagement":
        try:
            from app.engagements import steps as engagement_steps
        except ModuleNotFoundError:
            return []
        return list(engagement_steps.STEPS)
    if mode == "existing_company":
        try:
            from app.discovery import steps as company_steps
        except ModuleNotFoundError:
            return []
        return list(company_steps.STEPS)
    if mode == "surprise":
        try:
            from app.idealab import steps as idealab_steps
        except ModuleNotFoundError:
            return []
        return list(idealab_steps.STEPS)
    return []


_STEPS: dict[str, list[StepDef]] = {}


def all_steps(mode: str = "idea") -> list[StepDef]:
    if mode not in _STEPS:
        _STEPS[mode] = _lazy_steps() if mode == "idea" else _mode_steps(mode)
    return _STEPS[mode]


def reset_steps_cache() -> None:
    _STEPS.clear()


def steps_for(phase: Phase, mode: str = "idea") -> list[StepDef]:
    return [s for s in all_steps(mode) if s.phase == phase]


def phase_index(phase: str, mode: str = "idea") -> int:
    phases = phases_for_mode(mode)
    try:
        return phases.index(phase)
    except ValueError:
        return 0


def narrate_keys() -> set[str]:
    keys = {s.narrate for s in all_steps()}
    keys.update(
        {
            "understand.search",
            "understand.search_skipped",
            "validate.auto_answered",
            "commit.team_capped",
            "build.reading_files",
            "build.editing_files",
            "build.writing_files",
            "build.running_commands",
        }
    )
    return keys
