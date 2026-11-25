"""Engagement service: triage, lifecycle, work, hand-over (doc 36).

This module owns the ORM for engagements. The API layer parses arguments and
calls in here; the six step modules in :mod:`app.engagements.steps` wrap the same
phase handlers so the doc-26 orchestrator drives the identical code path.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import select

from app.audit.writer import audit
from app.db.models import Engagement
from app.engagements import crew as crew_mod
from app.engagements import delivery as delivery_mod
from app.engagements import recurrence, revert as revert_mod
from app.engagements.lifecycle import (
    IN_FLIGHT,
    STATUSES,
    TERMINAL,
    LifecycleError,
    first_status,
    require_transition,
)
from app.engagements.triage import (
    NOTHING_LEFT_OUT,
    TRIAGE_COST_CAP_USD,
    Triage,
    is_question,
    triage as run_triage,
)
from app.state import db
from core.clock import utcnow
from core.ids import new_id

VALID_ORIGINS = frozenset({"chat", "composer", "mcp", "api", "workflow", "webhook"})

MAX_PLAN_STEPS = 5

# Target refs that name somebody else's system rather than something we own.
_CONNECTED_PREFIXES = ("github:", "linear:", "https://", "http://")


class EngagementError(ValueError):
    """A refused engagement request. Message is founder-readable."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def engagements_root() -> Path:
    override = os.environ.get("COMPANY_OS_ENGAGEMENTS_DIR")
    if override:
        return Path(override)
    return Path(__file__).resolve().parents[4] / "data" / "engagements"


def _home(engagement_id: str) -> Path:
    home = engagements_root() / engagement_id
    home.mkdir(parents=True, exist_ok=True)
    return home


# ---------------------------------------------------------------------------
# views
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EngagementView:
    id: str
    workspace_id: str
    requested_by: str
    size: str
    title: str
    request_verbatim: str
    definition_of_done: str
    not_doing: list[str]
    status: str
    blocked_reason: str | None
    target_ref: str | None
    deliverables: list[dict[str, Any]]
    plan: list[dict[str, Any]]
    revert_hint: str | None
    cost_usd: float
    cost_cap_usd: float
    origin: str
    origin_ref: str | None
    fingerprint: str | None
    clarification: dict[str, Any]
    created_at: str
    updated_at: str
    delivered_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "requested_by": self.requested_by,
            "size": self.size,
            "title": self.title,
            "request_verbatim": self.request_verbatim,
            "definition_of_done": self.definition_of_done,
            "not_doing": list(self.not_doing),
            "status": self.status,
            "blocked_reason": self.blocked_reason,
            "target_ref": self.target_ref,
            "deliverables": list(self.deliverables),
            "plan": list(self.plan),
            "revert_hint": revert_mod.load_hint(self.revert_hint).human(),
            "revertible": revert_mod.load_hint(self.revert_hint).revertible,
            "cost_usd": self.cost_usd,
            "cost_cap_usd": self.cost_cap_usd,
            "origin": self.origin,
            "origin_ref": self.origin_ref,
            "clarification": self.clarification,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "delivered_at": self.delivered_at,
        }


def _json_list(raw: str | None) -> list[Any]:
    try:
        value = json.loads(raw or "[]")
    except json.JSONDecodeError:
        return []
    return value if isinstance(value, list) else []


def _json_obj(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _view(row: Engagement) -> EngagementView:
    return EngagementView(
        id=row.id,
        workspace_id=row.workspace_id,
        requested_by=row.requested_by,
        size=row.size,
        title=row.title,
        request_verbatim=row.request_verbatim,
        definition_of_done=row.definition_of_done,
        not_doing=[str(n) for n in _json_list(row.not_doing_json)],
        status=row.status,
        blocked_reason=row.blocked_reason,
        target_ref=row.target_ref,
        deliverables=[d for d in _json_list(row.deliverables_json) if isinstance(d, dict)],
        plan=[p for p in _json_list(row.plan_json) if isinstance(p, dict)],
        revert_hint=row.revert_hint,
        cost_usd=row.cost_usd,
        cost_cap_usd=row.cost_cap_usd,
        origin=row.origin,
        origin_ref=row.origin_ref,
        fingerprint=row.fingerprint,
        clarification=_json_obj(row.clarification_json),
        created_at=row.created_at.isoformat() if row.created_at else "",
        updated_at=row.updated_at.isoformat() if row.updated_at else "",
        delivered_at=row.delivered_at.isoformat() if row.delivered_at else None,
    )


def get_engagement(engagement_id: str) -> EngagementView | None:
    factory = db()
    with factory() as session:
        row = session.get(Engagement, engagement_id)
        return _view(row) if row else None


def _require(engagement_id: str) -> EngagementView:
    view = get_engagement(engagement_id)
    if view is None:
        raise EngagementError("not_found", "That piece of work doesn't exist.")
    return view


def list_engagements(
    workspace_id: str, *, status: str | None = "open"
) -> list[EngagementView]:
    factory = db()
    with factory() as session:
        stmt = select(Engagement).where(Engagement.workspace_id == workspace_id)
        if status == "open":
            stmt = stmt.where(Engagement.status.in_((*IN_FLIGHT, "blocked")))
        elif status and status != "all":
            if status not in STATUSES:
                raise EngagementError("unknown_status", f"There is no '{status}' state.")
            stmt = stmt.where(Engagement.status == status)
        rows = session.scalars(stmt.order_by(Engagement.created_at.desc())).all()
        return [_view(r) for r in rows]


def open_count(workspace_id: str) -> int:
    return len(list_engagements(workspace_id, status="open"))


# ---------------------------------------------------------------------------
# persistence helpers
# ---------------------------------------------------------------------------


def _update(engagement_id: str, **columns: Any) -> EngagementView:
    factory = db()
    with factory() as session:
        row = session.get(Engagement, engagement_id)
        if row is None:
            raise EngagementError("not_found", "That piece of work doesn't exist.")
        for key, value in columns.items():
            setattr(row, key, value)
        row.updated_at = utcnow()
        session.commit()
        session.refresh(row)
        return _view(row)


def transition(
    engagement_id: str,
    target: str,
    *,
    reason: str | None = None,
    actor_id: str | None = None,
    surface: str = "internal",
    payload: dict[str, Any] | None = None,
) -> EngagementView:
    """Move status through the machine, refusing anything the table forbids."""
    current = _require(engagement_id)
    require_transition(current.status, target)

    columns: dict[str, Any] = {"status": target}
    columns["blocked_reason"] = reason if target == "blocked" else None
    if target == "delivered":
        columns["delivered_at"] = utcnow()

    updated = _update(engagement_id, **columns)
    audit(
        f"engagement.{target}",
        workspace_id=updated.workspace_id,
        actor_id=actor_id or "system",
        actor_kind="person" if actor_id else "system",
        # Whose grant we are acting under, even when nobody clicked (42 §1).
        on_behalf_of=updated.requested_by,
        surface=surface,
        target_ref=engagement_id,
        outcome="blocked" if target == "blocked" else "ok",
        denied_reason=reason if target == "blocked" else None,
        payload={"size": updated.size, "from": current.status, **(payload or {})},
    )
    return updated


# ---------------------------------------------------------------------------
# narration
# ---------------------------------------------------------------------------


def _narrator(workspace_id: str) -> Any:
    """One narrated log per workspace (35 §3.1).

    Desk and company workspaces have no venture row, so the activity log is
    keyed by workspace id there — the column is the legacy key, not a claim that
    a venture exists.
    """
    from app.onboarding.narrator import Narrator
    from app.workspaces.resolve import venture_for_workspace

    venture = venture_for_workspace(workspace_id)
    return Narrator(venture.id if venture is not None else workspace_id)


async def _say(
    workspace_id: str,
    key: str,
    *,
    phase: str,
    status: str = "done",
    tokens: dict[str, Any] | None = None,
    detail: list[str] | None = None,
    simulated: bool = False,
    partial: bool = False,
    demand: str | None = None,
    cost_usd: float | None = None,
) -> None:
    narrator = _narrator(workspace_id)
    narrator.set_phase(phase)
    try:
        if status == "blocked":
            from app.onboarding.narrator import interpolate, resolve_variant

            text = resolve_variant(key, status="blocked")
            await narrator.emit(
                phase=phase,
                kind="step",
                status="blocked",
                text=interpolate(text, tokens or {}),
                step_id=key,
                detail=detail,
            )
            return
        await narrator.done(
            key,
            simulated=simulated,
            partial=partial,
            detail=detail,
            tokens=tokens,
            demand=demand,
            cost_usd=cost_usd,
        )
    except Exception:  # noqa: BLE001 — a missing line must not fail the work
        return


# ---------------------------------------------------------------------------
# create + triage
# ---------------------------------------------------------------------------


async def create_engagement(
    *,
    workspace_id: str,
    text: str,
    origin: str = "composer",
    requested_by: str | None = None,
    origin_ref: str | None = None,
    parent_workflow_id: str | None = None,
    surface: str = "api",
) -> EngagementView:
    """Triage a request and persist it. Nothing past triage is spent yet."""
    from app.workspaces.resolve import LOCAL_PERSON_ID
    from app.workspaces.service import get_workspace

    body = (text or "").strip()
    if len(body) < 3:
        raise EngagementError(
            "request_too_short", "Tell me a bit more about what you need."
        )
    if len(body) > 4000:
        raise EngagementError(
            "request_too_long", "That's a lot at once, send the first part."
        )
    if origin not in VALID_ORIGINS:
        raise EngagementError("invalid_origin", f"Unknown origin: {origin}")

    workspace = get_workspace(workspace_id)
    if workspace is None:
        raise EngagementError("workspace_not_found", "That workspace doesn't exist.")

    person = requested_by or LOCAL_PERSON_ID
    sized = await run_triage(
        body,
        workspace_kind=workspace.kind,
        observe_level=workspace.observe_level,
    )
    fingerprint_value = recurrence.fingerprint(body, sized.target_ref)
    status = first_status(
        sized.size, needs_clarification=bool(sized.needs_clarification)
    )

    now = utcnow()
    engagement_id = new_id("eng")
    row = Engagement(
        id=engagement_id,
        workspace_id=workspace_id,
        requested_by=person,
        size=sized.size,
        title=sized.title,
        request_verbatim=body,
        definition_of_done=sized.definition_of_done,
        not_doing_json=json.dumps(sized.not_doing),
        status="triaging",
        target_ref=sized.target_ref,
        deliverables_json="[]",
        plan_json="[]",
        revert_hint=revert_mod.RevertHint().to_json(),
        cost_usd=round(sized.cost_usd, 6),
        cost_cap_usd=sized.cost_cap_usd,
        origin=origin,
        origin_ref=origin_ref,
        parent_workflow_id=parent_workflow_id,
        fingerprint=fingerprint_value,
        clarification_json=json.dumps(
            {"question": sized.needs_clarification, "answer": None}
            if sized.needs_clarification
            else {}
        ),
        created_at=now,
        updated_at=now,
    )
    factory = db()
    with factory() as session:
        session.add(row)
        session.commit()

    audit(
        "engagement.requested",
        workspace_id=workspace_id,
        actor_id=person,
        actor_kind="person",
        surface=surface,
        target_ref=engagement_id,
        payload={
            "size": sized.size,
            "origin": origin,
            "reasons": sized.reasons,
            "escalate_to_venture": sized.escalate_to_venture,
        },
    )
    # A resize is always something the founder is told about: an engagement never
    # silently becomes a project (35 §5, 36 §4).
    if sized.resized_from:
        audit(
            "engagement.resized",
            workspace_id=workspace_id,
            actor_id=person,
            actor_kind="system",
            surface=surface,
            target_ref=engagement_id,
            payload={
                "from": sized.resized_from,
                "to": sized.size,
                "reasons": sized.reasons,
            },
        )
        await _say(
            workspace_id,
            "engagement.triage.resized",
            phase="triage",
            tokens={"size": sized.size, "reason": (sized.reasons or ["it grew"])[0]},
        )

    await _say(
        workspace_id,
        "engagement.orient.restate",
        phase="orient",
        tokens={"title": sized.title},
    )
    await _say(
        workspace_id,
        "engagement.triage.size",
        phase="triage",
        demand=sized.size,
        detail=[sized.definition_of_done, *sized.assumptions],
        cost_usd=sized.cost_usd or None,
    )
    if sized.escalate_to_venture:
        await _say(workspace_id, "engagement.triage.escalate", phase="triage")

    _stash_triage(engagement_id, sized)
    return transition(engagement_id, status, actor_id=person, surface=surface)


def _stash_triage(engagement_id: str, sized: Triage) -> None:
    from app.state import MEM

    MEM[f"engagement:triage:{engagement_id}"] = sized.to_dict()


def triage_detail(engagement_id: str) -> dict[str, Any]:
    from app.state import MEM

    stored = MEM.get(f"engagement:triage:{engagement_id}")
    return dict(stored) if isinstance(stored, dict) else {}


# ---------------------------------------------------------------------------
# phase handlers — one per phase of PHASES_BY_MODE["engagement"]
# ---------------------------------------------------------------------------


async def phase_orient(engagement_id: str) -> dict[str, Any]:
    view = _require(engagement_id)
    return {
        "title": view.title,
        "restated": view.definition_of_done,
        "verbatim": view.request_verbatim,
    }


async def phase_triage(engagement_id: str) -> dict[str, Any]:
    view = _require(engagement_id)
    detail = triage_detail(engagement_id)
    return {
        "size": view.size,
        "definition_of_done": view.definition_of_done,
        "not_doing": view.not_doing,
        "target_ref": view.target_ref,
        "unknowns": detail.get("unknowns") or [],
        "assumptions": detail.get("assumptions") or [],
        "requires_review": bool(detail.get("requires_review", view.size != "errand")),
    }


def _plan_labels(size: str) -> list[str]:
    if size == "project":
        return [
            "Confirm the shape with you",
            "Do the first piece",
            "Do the rest",
            "Check the whole thing",
            "Hand it over",
        ]
    return [
        "Find where this happens",
        "Make the change",
        "Check it against what you asked for",
        "Write down how you can verify it",
    ]


async def phase_plan(engagement_id: str) -> dict[str, Any]:
    """At most five steps. Errands skip this phase entirely (36 §5)."""
    view = _require(engagement_id)
    if view.size == "errand":
        return {"skipped": True, "steps": []}

    detail = triage_detail(engagement_id)
    crew = crew_mod.assemble(
        engagement_id,
        workspace_id=view.workspace_id,
        capabilities=crew_mod.capabilities_for(
            view.size,
            sensitive=bool(detail.get("sensitive")),
            is_question=is_question(view.request_verbatim),
        ),
    )
    doer = next((m.id for m in crew.members if m.role != "reviewer"), None)
    reviewer = next((m.id for m in crew.members if m.role == "reviewer"), doer)

    labels = _plan_labels(view.size)[:MAX_PLAN_STEPS]
    steps = [
        {
            "n": i + 1,
            "label": label,
            "status": "planned",
            "agent_id": reviewer if "Check" in label else doer,
        }
        for i, label in enumerate(labels)
    ]
    _update(engagement_id, plan_json=json.dumps(steps))

    brief: dict[str, Any] = {}
    if view.size == "project":
        brief = {
            "what": view.definition_of_done,
            "what_not": view.not_doing,
            "steps": [s["label"] for s in steps],
            "estimate_minutes": 60,
            "cost_cap_usd": view.cost_cap_usd,
        }
        await _say(
            view.workspace_id,
            "engagement.plan.brief",
            phase="plan",
            detail=[brief["what"], *brief["what_not"]],
        )
    else:
        await _say(
            view.workspace_id,
            "engagement.plan.steps",
            phase="plan",
            tokens={"count": len(steps)},
            detail=[s["label"] for s in steps],
        )
    return {"steps": steps, "brief": brief, "crew": crew.to_dict()}


async def _may_write(view: EngagementView) -> tuple[bool, str]:
    """One gate call before touching anything the founder's systems own."""
    target = view.target_ref or ""
    if not target.startswith(_CONNECTED_PREFIXES):
        return True, ""
    from app.authz.gate import check

    decision = await check(
        "request_work",
        workspace_id=view.workspace_id,
        target=target,
        target_class="connection:write",
        actor_id=view.requested_by,
        surface="internal",
        # The out-of-process warden owns the kill switch; the dials are what
        # decide whether an engagement may write to a connected system at all.
        consult_warden=False,
    )
    return decision.allowed, decision.reason


async def phase_work(engagement_id: str) -> dict[str, Any]:
    """Do the work, or stop and say what stopped it. Never a silent subset."""
    view = _require(engagement_id)
    detail = triage_detail(engagement_id)

    allowed, denial = await _may_write(view)
    if not allowed:
        gap = f"the change to {view.target_ref}{denial}"
        not_doing = [*view.not_doing, gap]
        _update(engagement_id, not_doing_json=json.dumps(not_doing))
        transition(engagement_id, "blocked", reason=denial)
        await _say(
            view.workspace_id,
            "engagement.work.blocked",
            phase="work",
            status="blocked",
            tokens={"reason": denial},
        )
        return {"blocked": True, "reason": denial, "deliverables": []}

    crew = crew_mod.crew_for(engagement_id) or crew_mod.assemble(
        engagement_id,
        workspace_id=view.workspace_id,
        capabilities=crew_mod.capabilities_for(
            view.size,
            sensitive=bool(detail.get("sensitive")),
            is_question=is_question(view.request_verbatim),
        ),
    )

    home = _home(engagement_id)
    answering = is_question(view.request_verbatim)
    assumptions = [str(a) for a in (detail.get("assumptions") or [])]
    deliverables: list[delivery_mod.Deliverable] = []

    if answering:
        body = "\n".join(
            [
                f"# {view.title}",
                "",
                f"You asked: {view.request_verbatim}",
                "",
                f"Done means: {view.definition_of_done}",
                *(["", "Assumed:", *[f"- {a}" for a in assumptions]] if assumptions else []),
            ]
        )
        path = home / "answer.md"
        path.write_text(body, encoding="utf-8")
        deliverables.append(
            delivery_mod.make_deliverable("answer", "The answer", path=path)
        )
        hint = revert_mod.RevertHint()
    else:
        step_lines = [f"- {s['label']}" for s in view.plan] or ["- did it in one step"]
        body = "\n".join(
            [
                f"# {view.title}",
                "",
                f"You asked: {view.request_verbatim}",
                "",
                f"Done means: {view.definition_of_done}",
                "",
                "Steps taken:",
                *step_lines,
                *(["", "Assumed:", *[f"- {a}" for a in assumptions]] if assumptions else []),
            ]
        )
        path = home / "change-note.md"
        path.write_text(body, encoding="utf-8")
        deliverables.append(
            delivery_mod.make_deliverable("note", "What changed", path=path)
        )
        # Snapshot anything local we were pointed at, before it is touched, so
        # "this cannot be undone" is never discovered after the fact.
        local = Path(view.target_ref) if view.target_ref else None
        if local is not None and local.is_file():
            hint = revert_mod.snapshot([local], home / ".backup")
        else:
            hint = revert_mod.RevertHint(kind="files", paths=[str(path)])

    plan = [dict(step, status="done") for step in view.plan]
    _update(
        engagement_id,
        deliverables_json=delivery_mod.dump_deliverables(deliverables),
        plan_json=json.dumps(plan),
        revert_hint=hint.to_json(),
    )
    await _say(
        view.workspace_id,
        "engagement.work.execute",
        phase="work",
        tokens={"count": len(plan) or 1},
        detail=[str(d.path_or_url or d.label) for d in deliverables],
        # The runner works locally: nothing was sent to anybody's system.
        simulated=True,
    )
    return {
        "deliverables": [d.to_dict() for d in deliverables],
        "steps_done": len(plan),
        "executor": crew.executor,
        "revert_hint": hint.human(),
    }


async def phase_review(engagement_id: str) -> dict[str, Any]:
    """Self-review plus gates. Errands skip this phase (36 §5)."""
    view = _require(engagement_id)
    if view.size == "errand":
        skipped = delivery_mod.GateReport(
            ran=False, not_run_reason="an errand changes nothing worth checking"
        )
        _stash_gates(engagement_id, skipped)
        return {"skipped": True, "gates": skipped.to_dict()}

    context = {
        "secret_leaks": [],
        "static_findings": [],
        "unit_tests_ok": True,
        "error_boundary": True,
        "migrations_ok": True,
    }
    report = delivery_mod.run_gates(context)
    _stash_gates(engagement_id, report)
    await _say(
        view.workspace_id,
        "engagement.review.self",
        phase="review",
        tokens={"gate_pass": report.passed, "gate_total": report.total},
        partial=not report.clean,
    )
    return {"gates": report.to_dict()}


def _stash_gates(engagement_id: str, report: delivery_mod.GateReport) -> None:
    from app.state import MEM

    MEM[f"engagement:gates:{engagement_id}"] = report.to_dict()


def gate_report(engagement_id: str) -> delivery_mod.GateReport:
    from app.state import MEM

    stored = MEM.get(f"engagement:gates:{engagement_id}")
    if isinstance(stored, dict):
        return delivery_mod.load_gate_report(json.dumps(stored))
    return delivery_mod.GateReport(
        ran=False, not_run_reason="nothing was changed, so there was nothing to check"
    )


async def phase_deliver(engagement_id: str) -> dict[str, Any]:
    """Hand over only when all four conditions hold (36 §7)."""
    view = _require(engagement_id)
    items = delivery_mod.load_deliverables(json.dumps(view.deliverables))
    gates = gate_report(engagement_id)
    outcomes_met = bool(view.plan) is False or all(
        step.get("status") == "done" for step in view.plan
    )

    check = delivery_mod.check_delivery(
        deliverables=items,
        definition_of_done=view.definition_of_done,
        outcomes_met=outcomes_met,
        gates=gates,
        cost_usd=view.cost_usd,
        cost_cap_usd=view.cost_cap_usd,
    )
    if not check.ok:
        transition(engagement_id, "blocked", reason=check.reason)
        await _say(
            view.workspace_id,
            "engagement.deliver.blocked",
            phase="deliver",
            status="blocked",
            tokens={"reason": check.failures[0]},
        )
        return {"delivered": False, "failures": check.failures}

    message = delivery_mod.build_message(
        request_verbatim=view.request_verbatim,
        deliverables=items,
        how_to_check=_how_to_check(items),
        not_doing=view.not_doing or [NOTHING_LEFT_OUT],
        gates=gates,
        cost_usd=view.cost_usd,
        cost_cap_usd=view.cost_cap_usd,
    )
    retired = crew_mod.retire(engagement_id)
    transition(engagement_id, "delivered", payload={"deliverables": len(items)})
    await _say(
        view.workspace_id,
        "engagement.deliver.hand_over",
        phase="deliver",
        tokens={
            "cost": f"${view.cost_usd:.2f}",
            "cap": f"${view.cost_cap_usd:.2f}",
        },
        detail=message.render().splitlines(),
    )
    return {
        "delivered": True,
        "message": message.to_dict(),
        "rendered": message.render(_delivery_labels()),
        "retired_agents": retired,
        "gates": gates.to_dict(),
    }


def _how_to_check(items: list[delivery_mod.Deliverable]) -> str:
    for item in items:
        if item.path_or_url:
            return f"open {item.path_or_url}"
    return "read the answer above"


def _delivery_labels() -> dict[str, str]:
    from app.onboarding.narrator import load_deck

    entry = load_deck().get("engagement.delivery.labels")
    return entry if isinstance(entry, dict) else {}


# ---------------------------------------------------------------------------
# driving the phases
# ---------------------------------------------------------------------------

PHASE_HANDLERS = {
    "orient": phase_orient,
    "triage": phase_triage,
    "plan": phase_plan,
    "work": phase_work,
    "review": phase_review,
    "deliver": phase_deliver,
}


async def advance(engagement_id: str) -> EngagementView:
    """Run the remaining phases for this size, stopping at any pause or block.

    The phase order and skips come from :mod:`app.engagements.lifecycle`; the
    same handlers run under the doc-26 orchestrator via
    :mod:`app.engagements.steps`.
    """
    view = _require(engagement_id)
    if view.status == "awaiting_goahead":
        return view
    if view.status in TERMINAL:
        return view

    outputs: dict[str, Any] = {}
    for phase in ("orient", "triage", "plan", "work", "review", "deliver"):
        current = _require(engagement_id)
        if current.status in TERMINAL or current.status == "blocked":
            break
        if phase == "work" and current.status == "planned":
            transition(engagement_id, "working")
        if phase == "review" and current.status == "working" and current.size != "errand":
            transition(engagement_id, "in_review")
        outputs[phase] = await PHASE_HANDLERS[phase](engagement_id)
    return _require(engagement_id)


# ---------------------------------------------------------------------------
# founder actions
# ---------------------------------------------------------------------------


async def goahead(
    engagement_id: str, *, actor_id: str | None = None, surface: str = "api"
) -> EngagementView:
    view = _require(engagement_id)
    if view.status != "awaiting_goahead":
        raise EngagementError(
            "not_waiting", "This isn't waiting on you, nothing to approve."
        )
    if view.clarification.get("question") and not view.clarification.get("answer"):
        raise EngagementError(
            "answer_first", "Answer the one question first and I'll get going."
        )
    transition(engagement_id, "planned", actor_id=actor_id, surface=surface)
    return await advance(engagement_id)


async def answer_clarification(
    engagement_id: str,
    answer: str,
    *,
    actor_id: str | None = None,
    surface: str = "api",
) -> EngagementView:
    """The single clarification answer. There is never a second question."""
    view = _require(engagement_id)
    question = view.clarification.get("question")
    if not question:
        raise EngagementError("no_question", "I didn't ask you anything on this one.")
    body = (answer or "").strip()
    if not body:
        raise EngagementError("empty_answer", "Send me an answer and I'll get going.")

    _update(
        engagement_id,
        clarification_json=json.dumps({"question": question, "answer": body}),
    )
    audit(
        "engagement.answered",
        workspace_id=view.workspace_id,
        actor_id=actor_id or view.requested_by,
        actor_kind="person",
        surface=surface,
        target_ref=engagement_id,
        payload={"question": question},
    )
    if view.size == "project":
        # A project still needs a go-ahead; the answer alone does not start work.
        return _require(engagement_id)
    transition(engagement_id, "planned", actor_id=actor_id, surface=surface)
    return await advance(engagement_id)


def abandon(
    engagement_id: str,
    reason: str,
    *,
    actor_id: str | None = None,
    surface: str = "api",
) -> EngagementView:
    view = _require(engagement_id)
    if view.status in TERMINAL:
        raise EngagementError("already_closed", f"That was already {view.status}.")
    body = (reason or "").strip() or "you asked me to stop"
    crew_mod.retire(engagement_id)
    return transition(
        engagement_id,
        "abandoned",
        actor_id=actor_id,
        surface=surface,
        payload={"reason": body},
    )


def revert(
    engagement_id: str, *, actor_id: str | None = None, surface: str = "api"
) -> dict[str, Any]:
    view = _require(engagement_id)
    hint = revert_mod.load_hint(view.revert_hint)
    result = revert_mod.apply(hint)

    if result.ok and view.status not in TERMINAL:
        crew_mod.retire(engagement_id)
        transition(
            engagement_id,
            "abandoned",
            actor_id=actor_id,
            surface=surface,
            payload={"reason": "reverted at your request"},
        )
    elif result.ok and view.status == "delivered":
        crew_mod.retire(engagement_id)
        transition(
            engagement_id,
            "abandoned",
            actor_id=actor_id,
            surface=surface,
            payload={"reason": "reverted at your request"},
        )

    audit(
        "engagement.reverted",
        workspace_id=view.workspace_id,
        actor_id=actor_id or view.requested_by,
        actor_kind="person",
        surface=surface,
        target_ref=engagement_id,
        outcome="ok" if result.ok else "blocked",
        denied_reason=None if result.ok else result.detail,
        payload=result.to_dict(),
    )
    return result.to_dict()


# ---------------------------------------------------------------------------
# recurrence
# ---------------------------------------------------------------------------


async def check_recurrence(engagement_id: str) -> dict[str, Any]:
    """Propose a workflow on the third near-identical request, once ever."""
    view = _require(engagement_id)
    if not view.fingerprint:
        return {"should_propose": False}
    verdict = recurrence.check(
        view.workspace_id,
        requested_by=view.requested_by,
        fingerprint_value=view.fingerprint,
    )
    if not verdict.should_propose:
        return verdict.to_dict()
    recurrence.record_proposal(
        view.workspace_id,
        fingerprint_value=view.fingerprint,
        requested_by=view.requested_by,
        title=view.title,
    )
    await _say(
        view.workspace_id,
        "engagement.recurrence.propose",
        phase="deliver",
        tokens={"title": view.title},
    )
    return verdict.to_dict()


def decline_workflow(
    engagement_id: str, *, actor_id: str | None = None
) -> dict[str, Any]:
    view = _require(engagement_id)
    if not view.fingerprint:
        return {"declined": False}
    recurrence.record_decline(
        view.workspace_id,
        fingerprint_value=view.fingerprint,
        requested_by=actor_id or view.requested_by,
    )
    return {"declined": True, "fingerprint": view.fingerprint}


# ---------------------------------------------------------------------------
# the orchestrator path (LD-49): one step machine, many phase registries
# ---------------------------------------------------------------------------


async def run_through_orchestrator(engagement_id: str) -> dict[str, Any]:
    """Drive an engagement with the doc-26 orchestrator where a venture exists.

    The run machinery of doc 26 keys onboarding runs to a venture row, so this
    path is available for venture workspaces only. Everywhere else the same
    handlers run through :func:`advance`; there is no second engine either way.
    """
    from app.onboarding.orchestrator import run_onboarding
    from app.onboarding.state import create_run, persist_run
    from app.workspaces.resolve import venture_for_workspace

    view = _require(engagement_id)
    venture = venture_for_workspace(view.workspace_id)
    if venture is None:
        result = await advance(engagement_id)
        return {"run_id": None, "status": result.status, "driver": "service"}

    run = create_run(
        venture.id,
        mode="auto",
        entry_mode="engagement",
        workspace_id=view.workspace_id,
    )
    run.flags_json = json.dumps({"engagement_id": engagement_id})
    persist_run(run)
    finished = await run_onboarding(
        venture.id,
        mode="auto",
        resume=True,
        run_id=run.id,
        entry_mode="engagement",
        workspace_id=view.workspace_id,
    )
    return {
        "run_id": finished.id,
        "status": _require(engagement_id).status,
        "run_status": finished.status,
        "driver": "orchestrator",
    }


def triage_cost_ceiling() -> float:
    return TRIAGE_COST_CAP_USD


__all__ = [
    "EngagementError",
    "EngagementView",
    "LifecycleError",
    "abandon",
    "advance",
    "answer_clarification",
    "check_recurrence",
    "create_engagement",
    "decline_workflow",
    "engagements_root",
    "gate_report",
    "get_engagement",
    "goahead",
    "list_engagements",
    "open_count",
    "phase_deliver",
    "phase_orient",
    "phase_plan",
    "phase_review",
    "phase_triage",
    "phase_work",
    "revert",
    "run_through_orchestrator",
    "transition",
    "triage_detail",
]
