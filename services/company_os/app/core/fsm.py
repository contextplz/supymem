"""Table-driven state machines (docs/supymem/05 §3).

Illegal transitions raise ``IllegalTransition`` (HTTP 409 at the API boundary).
Existing onboarding still uses ``app.core.stages`` / legacy ``VentureStage``;
new Supymem entities use this module.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, NamedTuple

from core.errors import IllegalTransition

Guard = Callable[[Any], bool] | None


class Transition(NamedTuple):
    src: frozenset[str]
    dst: str
    guard: Guard = None
    effects: list[str] = []


def _edge(src: str | frozenset[str], dst: str, *, guard: Guard = None, effects: list[str] | None = None) -> Transition:
    sources = frozenset({src}) if isinstance(src, str) else frozenset(src)
    return Transition(src=sources, dst=dst, guard=guard, effects=effects or [])


def _star(states: frozenset[str], dst: str) -> Transition:
    return _edge(states, dst)


# --- 3.1 Venture (unified vocabulary) ---------------------------------------

VENTURE_STATES = frozenset(
    {
        "created",
        "ideating",
        "researching",
        "planning",
        "estimating",
        "awaiting_credits",
        "approved",
        "building",
        "qa",
        "deploying",
        "live",
        "operating",
        "iterating",
        "scaling",
        "paused",
        "archived",
    }
)

_VENTURE_LINE = (
    "created",
    "ideating",
    "researching",
    "planning",
    "estimating",
    "awaiting_credits",
    "approved",
    "building",
    "qa",
    "deploying",
    "live",
)
VENTURE_TRANSITIONS: list[Transition] = [
    *[_edge(_VENTURE_LINE[i], _VENTURE_LINE[i + 1]) for i in range(len(_VENTURE_LINE) - 1)],
    _edge("live", "operating"),
    _edge("operating", "iterating"),
    _edge("iterating", "operating"),
    _edge("operating", "scaling"),
    _edge("scaling", "operating"),
    _edge("operating", "paused"),
    _edge("paused", "operating"),
    _edge(VENTURE_STATES - {"archived"}, "archived"),
]

# Legacy Company OS stages → unified vocabulary (doc 05 §3.1 + Stage 1 plan).
LEGACY_VENTURE_STAGE: dict[str, str] = {
    "draft": "created",
    "classifying": "ideating",
    "clarifying": "ideating",
    "charter_review": "planning",
    "org_planning": "planning",
    "fork_choice": "planning",
    "building": "building",
    "demand_testing": "iterating",
    "readiness_review": "qa",
    "hosting": "deploying",
    "live": "live",
    "pivot": "iterating",
    "paused": "paused",
    "archived": "archived",
}


# --- 3.2 IdeaSession --------------------------------------------------------

IDEA_SESSION_STATES = frozenset(
    {"open", "recon", "clarifying", "proposing", "reviewing", "validating", "confirmed", "abandoned"}
)
_IDEA_ACTIVE = IDEA_SESSION_STATES - {"confirmed", "abandoned"}
IDEA_SESSION_TRANSITIONS: list[Transition] = [
    _edge("open", "recon"),
    _edge("recon", "clarifying"),
    _edge("clarifying", "proposing"),
    _edge("proposing", "reviewing"),
    _edge("reviewing", "confirmed"),
    _edge("clarifying", "validating"),
    _edge("validating", "reviewing"),
    _edge("validating", "clarifying"),
    _edge("reviewing", "clarifying"),
    _edge("proposing", "clarifying"),
    _edge(_IDEA_ACTIVE, "abandoned"),
]


# --- 3.3 ExecutionPlan ------------------------------------------------------

PLAN_STATES = frozenset(
    {"draft", "founder_review", "editing", "approved", "executing", "completed", "superseded", "discarded"}
)
PLAN_TRANSITIONS: list[Transition] = [
    _edge("draft", "founder_review"),
    _edge("founder_review", "editing"),
    _edge("editing", "founder_review"),
    _edge("founder_review", "approved"),
    _edge("approved", "superseded"),
    _edge("approved", "executing"),
    _edge("executing", "completed"),
    _edge(frozenset({"draft", "founder_review", "editing"}), "discarded"),
]


# --- 3.4 PlanMilestone / PlanTask ------------------------------------------

MILESTONE_STATES = frozenset(
    {"pending", "ready", "running", "qa", "review", "done", "waiting_for_founder", "blocked", "failed", "out_of_scope"}
)
MILESTONE_TRANSITIONS: list[Transition] = [
    _edge("pending", "ready"),
    _edge("ready", "running"),
    _edge("running", "qa"),
    _edge("qa", "review"),
    _edge("review", "done"),
    _edge("review", "failed"),
    _edge("running", "waiting_for_founder"),
    _edge("waiting_for_founder", "ready"),
    _edge("running", "blocked"),
    _edge("blocked", "ready"),
    _edge(frozenset({"running", "qa"}), "failed"),
    _edge("failed", "ready"),
    _edge(frozenset({"running", "qa", "failed"}), "out_of_scope"),
]

TASK_STATES = MILESTONE_STATES - {"review"}
TASK_TRANSITIONS: list[Transition] = [
    t for t in MILESTONE_TRANSITIONS if t.dst != "review" and "review" not in t.src
] + [_edge("qa", "waiting_for_founder"), _edge("waiting_for_founder", "done"), _edge("waiting_for_founder", "failed")]


# --- 3.5 App ----------------------------------------------------------------

APP_STATES = frozenset(
    {
        "draft",
        "provisioning",
        "building",
        "deployed",
        "live",
        "sleeping",
        "degraded",
        "suspended",
        "deleting",
        "deleted",
    }
)
APP_TRANSITIONS: list[Transition] = [
    _edge("draft", "provisioning"),
    _edge("provisioning", "building"),
    _edge("building", "deployed"),
    _edge("deployed", "live"),
    _edge("live", "sleeping"),
    _edge("sleeping", "live"),
    _edge("live", "degraded"),
    _edge("degraded", "live"),
    _edge("live", "suspended"),
    _edge("suspended", "live"),
    _edge(APP_STATES - {"deleted", "deleting"}, "deleting"),
    _edge("deleting", "deleted"),
]


# --- 3.6 BuildRun -----------------------------------------------------------

BUILD_ACTIVE = frozenset(
    {
        "queued",
        "reserving_credits",
        "provisioning_sandbox",
        "running",
        "syncing_artifacts",
        "needs_input",
    }
)
BUILD_STATES = BUILD_ACTIVE | {"succeeded", "rejected", "failed", "cancelled", "cancelling"}
BUILD_TRANSITIONS: list[Transition] = [
    _edge(BUILD_ACTIVE, "cancelling"),
    _edge("cancelling", "cancelled"),
    _edge("cancelling", "failed"),
    _edge("queued", "reserving_credits"),
    _edge("reserving_credits", "provisioning_sandbox"),
    _edge("provisioning_sandbox", "running"),
    _edge("running", "syncing_artifacts"),
    _edge("syncing_artifacts", "succeeded"),
    _edge("running", "needs_input"),
    _edge("needs_input", "running"),
    _edge(frozenset({"queued", "reserving_credits"}), "rejected"),
    _edge(BUILD_ACTIVE, "failed"),
    _edge(BUILD_ACTIVE, "cancelled"),
]


# --- 3.7 Deployment ---------------------------------------------------------

DEPLOY_STATES = frozenset(
    {
        "pending",
        "building_image",
        "packaging",
        "pushing",
        "creating_infra",
        "health_checking",
        "live",
        "failed",
        "rolled_back",
        "superseded",
    }
)
DEPLOY_TRANSITIONS: list[Transition] = [
    _edge("pending", "building_image"),
    _edge("pending", "packaging"),
    _edge("building_image", "pushing"),
    _edge("packaging", "pushing"),
    _edge("pushing", "creating_infra"),
    _edge("creating_infra", "health_checking"),
    _edge("health_checking", "live"),
    _edge("health_checking", "failed"),
    _edge("failed", "rolled_back"),
    _edge("live", "superseded"),
]


# --- 3.8 AgentRun (visible states) ------------------------------------------

AGENT_RUN_WORK = frozenset(
    {
        "planning",
        "researching",
        "building",
        "testing",
        "deploying",
        "monitoring",
        "waiting_approval",
        "waiting_founder",
        "blocked",
    }
)
AGENT_RUN_STATES = frozenset({"queued", "thinking"}) | AGENT_RUN_WORK | {
    "completed",
    "failed",
    "cancelled",
    "blocked_out_of_scope",
    "paused_budget",
}
AGENT_RUN_TRANSITIONS: list[Transition] = [
    _edge("queued", "thinking"),
    *[_edge("thinking", s) for s in AGENT_RUN_WORK],
    *[_edge(s, "thinking") for s in AGENT_RUN_WORK],
    *[_edge(s, "completed") for s in AGENT_RUN_WORK | {"thinking"}],
    *[_edge(s, "failed") for s in AGENT_RUN_WORK | {"thinking", "queued"}],
    *[_edge(s, "cancelled") for s in AGENT_RUN_WORK | {"thinking", "queued"}],
    *[_edge(s, "blocked_out_of_scope") for s in AGENT_RUN_WORK | {"thinking"}],
    *[_edge(s, "paused_budget") for s in AGENT_RUN_WORK | {"thinking"}],
]


# --- 3.9 Job ----------------------------------------------------------------

JOB_STATES = frozenset({"scheduled", "leased", "succeeded", "retry_wait", "failed_dead", "cancelled"})
JOB_TRANSITIONS: list[Transition] = [
    _edge("scheduled", "leased"),
    _edge("leased", "succeeded"),
    _edge("leased", "retry_wait"),
    _edge("retry_wait", "scheduled"),
    _edge("leased", "failed_dead"),
    _edge("scheduled", "cancelled"),
]

# Compatibility for the existing JobQueue vocab until Stage 2 switches callers.
JOB_STATUS_TO_FSM = {
    "pending": "scheduled",
    "running": "leased",
    "done": "succeeded",
    "failed": "failed_dead",
    "poison": "failed_dead",
    "scheduled": "scheduled",
    "leased": "leased",
    "succeeded": "succeeded",
    "retry_wait": "retry_wait",
    "failed_dead": "failed_dead",
    "cancelled": "cancelled",
}


# --- 3.10 Approval ----------------------------------------------------------

APPROVAL_STATES = frozenset({"pending", "approved", "denied", "expired", "consumed"})
APPROVAL_TRANSITIONS: list[Transition] = [
    _edge("pending", "approved"),
    _edge("pending", "denied"),
    _edge("pending", "expired"),
    _edge("approved", "consumed"),
]

APPROVAL_KINDS = frozenset(
    {
        "idea_confirm",
        "plan_approve",
        "milestone_review",
        "credit_reserve_large",
        "production_deploy",
        "infra_scale_up",
        "external_publish",
        "delete_app",
        "external_aws_action",
        "out_of_scope_override",
        "terminal_command",
    }
)


# --- 3.11 CreditReservation -------------------------------------------------

RESERVATION_STATES = frozenset({"active", "open", "settled", "released", "expired"})
RESERVATION_TRANSITIONS: list[Transition] = [
    _edge(frozenset({"active", "open"}), "settled"),
    _edge(frozenset({"active", "open"}), "released"),
    _edge(frozenset({"active", "open"}), "expired"),
]


# --- 3.12 AppDomain ---------------------------------------------------------

DOMAIN_STATES = frozenset({"pending", "validating", "active", "failed", "removed"})
DOMAIN_TRANSITIONS: list[Transition] = [
    _edge("pending", "validating"),
    _edge("validating", "active"),
    _edge("active", "removed"),
    _edge("validating", "failed"),
    _edge("failed", "pending"),
]


# --- 3.13 Experiment --------------------------------------------------------

EXPERIMENT_STATES = frozenset({"proposed", "running", "concluded", "aborted"})
EXPERIMENT_TRANSITIONS: list[Transition] = [
    _edge("proposed", "running"),
    _edge("running", "concluded"),
    _edge("running", "aborted"),
]


MACHINES: dict[str, list[Transition]] = {
    "venture": VENTURE_TRANSITIONS,
    "idea_session": IDEA_SESSION_TRANSITIONS,
    "execution_plan": PLAN_TRANSITIONS,
    "plan_milestone": MILESTONE_TRANSITIONS,
    "plan_task": TASK_TRANSITIONS,
    "app": APP_TRANSITIONS,
    "build_run": BUILD_TRANSITIONS,
    "deployment": DEPLOY_TRANSITIONS,
    "agent_run": AGENT_RUN_TRANSITIONS,
    "job": JOB_TRANSITIONS,
    "approval": APPROVAL_TRANSITIONS,
    "credit_reservation": RESERVATION_TRANSITIONS,
    "app_domain": DOMAIN_TRANSITIONS,
    "experiment": EXPERIMENT_TRANSITIONS,
}

STATE_ATTR: dict[str, str] = {
    "venture": "stage",
    "job": "status",
    "approval": "status",
    "credit_reservation": "status",
}


def states_of(machine: str) -> frozenset[str]:
    found: set[str] = set()
    for t in MACHINES[machine]:
        found |= set(t.src)
        found.add(t.dst)
    return frozenset(found)


def legal_edges(machine: str) -> set[tuple[str, str]]:
    edges: set[tuple[str, str]] = set()
    for t in MACHINES[machine]:
        for src in t.src:
            edges.add((src, t.dst))
    return edges


def can_transition(machine: str, current: str, nxt: str) -> bool:
    cur = JOB_STATUS_TO_FSM.get(current, current) if machine == "job" else current
    if machine == "venture":
        cur = LEGACY_VENTURE_STAGE.get(cur, cur)
    return (cur, nxt) in legal_edges(machine)


def transition(
    session: Any,
    entity: Any,
    to_state: str,
    *,
    actor: str,
    reason: str,
    meta: dict[str, Any] | None = None,
    machine: str | None = None,
) -> Any:
    """Assert the edge is legal, set state, append an event_log row when possible."""
    name = machine or getattr(entity, "__fsm__", None)
    if not name:
        raise IllegalTransition("entity has no __fsm__ and machine= was not passed")
    attr = STATE_ATTR.get(name, "state")
    current = getattr(entity, attr)
    if name == "job":
        current = JOB_STATUS_TO_FSM.get(current, current)
    if name == "venture":
        current = LEGACY_VENTURE_STAGE.get(current, current)
    if not can_transition(name, current, to_state):
        raise IllegalTransition(
            f"illegal {name} transition {current} → {to_state}",
            detail={"from": current, "to": to_state, "machine": name},
        )
    setattr(entity, attr, to_state)
    if hasattr(entity, "updated_at"):
        entity.updated_at = datetime.now(UTC).replace(tzinfo=None)
    if session is not None:
        _write_event(session, entity, name, current, to_state, actor=actor, reason=reason, meta=meta or {})
        if name == "idea_session":
            from app.analytics.observability import IDEA_STEPS, stage_step

            previous, following = IDEA_STEPS.get(current), IDEA_STEPS.get(to_state)
            if previous != following:
                if current == "open":
                    stage_step(session, "idea", "entered", key=entity.id,
                               venture_id=entity.venture_id, workspace_id=entity.workspace_id)
                if previous and to_state != "abandoned" and to_state != "clarifying":
                    stage_step(session, previous, "completed", key=f"{entity.id}:{current}:{to_state}",
                               venture_id=entity.venture_id, workspace_id=entity.workspace_id)
                if following:
                    stage_step(session, following, "entered", key=f"{entity.id}:{current}:{to_state}",
                               venture_id=entity.venture_id, workspace_id=entity.workspace_id)
    return entity


def advance(
    session: Any,
    entity: Any,
    to_state: str,
    *,
    actor: str = "system",
    reason: str = "",
    meta: dict[str, Any] | None = None,
) -> Any:
    """Same as :func:`transition` with default actor/reason for in-process jobs."""
    return transition(
        session,
        entity,
        to_state,
        actor=actor,
        reason=reason or to_state,
        meta=meta,
    )


def _write_event(
    session: Any,
    entity: Any,
    machine: str,
    src: str,
    dst: str,
    *,
    actor: str,
    reason: str,
    meta: dict[str, Any],
) -> None:
    try:
        from core.ids import new_id

        from app.db.models import EventLog
    except Exception:
        return
    import json

    table = getattr(entity, "__tablename__", machine)
    session.add(
        EventLog(
            id=new_id("evt"),
            topic=f"{table}.state",
            venture_id=getattr(entity, "venture_id", None),
            actor=actor,
            payload_json=json.dumps(
                {"from": src, "to": dst, "reason": reason, "id": getattr(entity, "id", None), **meta}
            ),
            ts=datetime.now(UTC).replace(tzinfo=None),
        )
    )
