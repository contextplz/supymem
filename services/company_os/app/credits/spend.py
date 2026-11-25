"""Founder spend view and per-experiment caps (``spend_view_v1`` defaults on).

Settled spend comes from ``credit_ledger``; open reservations are shown separately.
Caps live in the experiment's ``context`` JSON, next to the existing run ceiling.
"""
from __future__ import annotations

import json
from dataclasses import replace
from datetime import datetime, timedelta

from sqlalchemy import and_, or_, select

from app.core.local_mode import is_local
from app.db.models import CreditAccount, CreditLedgerEntry, CreditReservation, Venture, Workspace, WorkspaceMember
from app.db.supymem_models import Experiment
from app.state import db
from core.clock import utcnow

CATEGORIES = ("experiment", "research", "chat", "outreach", "builds", "other")
_PREFIXES = (
    ("experiment", ("experiment.", "experiments.")),
    ("research", ("research.", "idea.", "simulation.")),
    ("chat", ("venture.chat.", "chat.", "voice.", "intake.", "onboarding")),
    ("outreach", ("outreach.", "outbound", "gtm.", "actuation.email", "inbox.")),
    ("builds", ("build.", "pagebuilder.", "app.", "device.")),
)
MICRO_PER_USD = 1_000_000
CAP_MESSAGE = "Your saved experiment limit stops {title}. Change the limit or stop this experiment, then choose what you want to run next."
CAP_SAVED_MESSAGE = "Your experiment setting is saved. Run it again when you are ready."


def enabled() -> bool:
    if is_local():
        return True
    from app.credits.policy import get_policy

    flags = get_policy("feature_flags", {})
    return isinstance(flags, dict) and flags.get("spend_view_v1", True) is True


def category(purpose: str | None) -> str:
    purpose = purpose or ""
    if is_local() and purpose.startswith("content."):
        return "content"
    return next((name for name, prefixes in _PREFIXES if purpose.startswith(prefixes)), "other")


def usd(micro: int | None) -> str:
    return f"${(micro or 0) / MICRO_PER_USD:,.2f}".replace(".00", "")


def _ctx(row: Experiment) -> dict:
    try:
        data = json.loads(row.context or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _run_ids(context: dict) -> set[str]:
    ids = set(context.get("spend_run_ids") or [])
    return ids | {context["run_id"]} if context.get("run_id") else ids


def _accounts(session, workspace_id: str, venture_id: str) -> list[str]:
    workspace = session.get(Workspace, workspace_id)
    venture = session.get(Venture, venture_id)
    if workspace is None or venture is None or venture.workspace_id != workspace_id:
        return []
    people = select(WorkspaceMember.person_id).where(WorkspaceMember.workspace_id == workspace_id)
    accounts = select(CreditAccount.id).where(or_(
        and_(CreditAccount.scope == "venture", CreditAccount.venture_id == venture_id),
        and_(CreditAccount.scope == "person", or_(CreditAccount.owner_person_id == workspace.owner_id,
                                                  CreditAccount.owner_person_id.in_(people)))))
    return list(session.scalars(accounts))


def _rows(session, workspace_id: str, venture_id: str) -> list[CreditLedgerEntry]:
    """All settled spend for this venture, independent of retry timestamps."""
    out = []
    # Indexed by ix_credit_ledger_account_created; a person wallet spans ventures, so keep this venture only.
    for row in session.scalars(select(CreditLedgerEntry).where(
            CreditLedgerEntry.account_id.in_(_accounts(session, workspace_id, venture_id)),
            CreditLedgerEntry.kind == "spend")):
        try:
            meta = json.loads(row.meta_json or "{}")
        except ValueError:
            continue
        if isinstance(meta, dict) and meta.get("venture_id") == venture_id and meta.get("workspace_id") in (workspace_id, None):
            out.append(row)
    return out


def _reservations(session, workspace_id: str, venture_id: str) -> list[CreditReservation]:
    if is_local():
        return []
    from app.credits.reserve import _HOLD

    return list(session.scalars(select(CreditReservation).join(
        CreditAccount, CreditAccount.id == CreditReservation.account_id).where(
        CreditReservation.account_id.in_(_accounts(session, workspace_id, venture_id)),
        CreditReservation.status.in_(_HOLD | {"reconciling"}),
        or_(CreditReservation.workspace_id == workspace_id, CreditReservation.workspace_id.is_(None)),
        or_(CreditReservation.venture_id == venture_id,
            and_(CreditReservation.venture_id.is_(None), CreditAccount.scope == "venture",
                 CreditAccount.venture_id == venture_id)))))


def _held_micro(row: CreditReservation) -> int:
    return row.remaining_micro if row.is_pool else row.amount_micro


def _periods(now: datetime) -> tuple[datetime, datetime]:
    day = now.replace(hour=0, minute=0, second=0, microsecond=0, tzinfo=None)
    return day - timedelta(days=day.weekday()), day.replace(day=1)


def _experiments(session, workspace_id: str, venture_id: str) -> list[Experiment]:
    from app.experiments.ledger import KIND

    return list(session.scalars(select(Experiment).where(
        Experiment.venture_id == venture_id, Experiment.workspace_id == workspace_id, Experiment.kind == KIND)))


def experiment_spent(session, workspace_id: str, venture_id: str, row: Experiment) -> int:
    """Lifetime spend settled against this experiment's runs."""
    ids = _run_ids(_ctx(row))
    if is_local():
        from app.credits.local_meter import run_cost
        return run_cost(workspace_id, venture_id, ids)
    if not ids:
        return 0
    return sum(-entry.amount_micro for entry in _rows(session, workspace_id, venture_id)
               if (entry.request_id or "").split(":", 1)[0] in ids)


def view(workspace_id: str, venture_id: str) -> dict:
    if is_local():
        from app.credits.local_meter import snapshot
        report = snapshot(workspace_id=workspace_id, venture_id=venture_id)
        with db()() as session:
            rows = []
            for row in _experiments(session, workspace_id, venture_id):
                context = _ctx(row)
                spent = experiment_spent(session, workspace_id, venture_id, row)
                cap = context.get("spend_cap_micro")
                rows.append({"id": row.id, "title": (context.get("spec") or {}).get("title") or row.hypothesis,
                    "spent_micro": spent, "cap_micro": cap, "reserved_micro": 0,
                    "remaining_micro": None if cap is None else max(0, cap - spent), "status": status(row, context, spent)})
        return {"venture_id": venture_id, **report, "experiments": rows}
    week_start, month_start = _periods(utcnow())
    with db()() as session:
        experiments = _experiments(session, workspace_id, venture_id)
        entries = _rows(session, workspace_id, venture_id)
        reservations = _reservations(session, workspace_id, venture_id)
    totals = {period: {name: 0 for name in CATEGORIES} for period in ("week", "month")}
    by_run: dict[str, dict[str, int]] = {}
    for entry in entries:
        amount, created = -entry.amount_micro, entry.created_at.replace(tzinfo=None)
        root = (entry.request_id or "").split(":", 1)[0]
        by_run.setdefault(root, {"week": 0, "month": 0, "total": 0})["total"] += amount
        for period, start in (("week", week_start), ("month", month_start)):
            if created >= start:
                totals[period][category(entry.purpose)] += amount
                by_run[root][period] += amount
    held = {name: 0 for name in CATEGORIES}
    held_by_run: dict[str, int] = {}
    for reservation in reservations:
        amount = _held_micro(reservation)
        held[category(reservation.purpose)] += amount
        root = (reservation.request_id or "").split(":", 1)[0]
        held_by_run[root] = held_by_run.get(root, 0) + amount
    rows = []
    for row in experiments:
        context = _ctx(row)
        spent = {key: sum(by_run.get(run, {}).get(key, 0) for run in _run_ids(context)) for key in ("week", "month", "total")}
        reserved = sum(held_by_run.get(run, 0) for run in _run_ids(context))
        cap = context.get("spend_cap_micro")
        if not any(spent.values()) and not reserved and cap is None:
            continue
        spec = context.get("spec") or {}
        rows.append({"id": row.id, "title": spec.get("title") or row.hypothesis, "status": status(row, context, spent["total"], reserved),
                     "spent_week_micro": spent["week"], "spent_month_micro": spent["month"],
                     "spent_micro": spent["total"], "cap_micro": cap, "reserved_micro": reserved,
                     "remaining_micro": None if cap is None else max(0, cap - spent["total"] - reserved)})
    return {"venture_id": venture_id, "week_start": week_start.isoformat(), "month_start": month_start.isoformat(),
            "week": {"total_micro": sum(totals["week"].values()), "by_category": totals["week"]},
            "month": {"total_micro": sum(totals["month"].values()), "by_category": totals["month"]},
            "reserved": {"total_micro": sum(held.values()), "by_category": held},
            "experiments": rows}


def status(row: Experiment, context: dict, spent: int, reserved: int = 0) -> str:
    if context.get("spend_stopped"):
        return "stopped"
    if context.get("spend_paused"):
        return "paused_at_cap"
    if row.state == "concluded":
        return "finished"
    cap = context.get("spend_cap_micro")
    if cap is not None and spent + reserved >= cap:
        return "at_cap"
    return "running" if row.state == "running" or context.get("claimed") else "not_started"


def _owned(session, workspace_id: str, venture_id: str, experiment_id: str) -> Experiment:
    from app.experiments.ledger import KIND

    row = session.get(Experiment, experiment_id)
    venture = session.get(Venture, venture_id)
    if (row is None or venture is None or venture.workspace_id != workspace_id or row.kind != KIND
            or (row.venture_id, row.workspace_id) != (venture_id, workspace_id)):
        raise KeyError("experiment")
    return row


def set_cap(workspace_id: str, venture_id: str, experiment_id: str, cap_micro: int | None, *, stop: bool = False) -> dict:
    if cap_micro is not None and (type(cap_micro) is not int or not 0 < cap_micro <= 100_000 * MICRO_PER_USD):
        raise ValueError("spend_cap_invalid")
    with db()() as session:
        row = _owned(session, workspace_id, venture_id, experiment_id)
        context = _ctx(row)
        context.pop("spend_paused", None)
        if stop:
            context["spend_stopped"] = True
        else:
            context.pop("spend_stopped", None)
            if cap_micro is None:
                context.pop("spend_cap_micro", None)
            else:
                context["spend_cap_micro"] = cap_micro
        row.context = json.dumps(context)
        session.commit()
        spent = experiment_spent(session, workspace_id, venture_id, row)
        ids = _run_ids(context)
        reserved = sum(_held_micro(hold) for hold in _reservations(session, workspace_id, venture_id)
                       if (hold.request_id or "").split(":", 1)[0] in ids)
        cap = context.get("spend_cap_micro")
        return {"id": row.id, "cap_micro": context.get("spend_cap_micro"), "spent_micro": spent,
                "reserved_micro": reserved, "remaining_micro": None if cap is None else max(0, cap - spent - reserved),
                "status": status(row, context, spent, reserved)}


def cap_block(workspace_id: str, venture_id: str, experiment_id: str, ceiling_micro: int, title: str) -> dict | None:
    """Pause a run whose confirmed ceiling would take the experiment past its founder cap.

    The run's existing ceiling bounds its own spend; open holds also use the remaining cap.
    """
    if not enabled():
        return None
    from app.chat.actions import get_action
    from app.chat.confirm import ConfirmationCard
    from core.copy import founder_copy

    with db()() as session:
        row = _owned(session, workspace_id, venture_id, experiment_id)
        context = _ctx(row)
        if context.get("spend_stopped"):
            return {"kind": "experiment_blocked", "actions": [], "citations": [], "body_md": founder_copy(
                f"You stopped {title}. No new runs will start. Work already in progress may still finish. "
                "Ask to change your experiment limit before running it again.")}
        cap = context.get("spend_cap_micro")
        if cap is None:
            return None
        spent = experiment_spent(session, workspace_id, venture_id, row)
        ids = _run_ids(context)
        reserved = sum(_held_micro(hold) for hold in _reservations(session, workspace_id, venture_id)
                       if (hold.request_id or "").split(":", 1)[0] in ids)
        if spent + reserved + (ceiling_micro or 0) <= cap:
            return None
        context["spend_paused"] = True
        row.context = json.dumps(context)
        session.commit()
    if is_local():
        return {"kind": "experiment_blocked", "citations": [], "actions": [],
            "body_md": founder_copy(f"Your saved experiment spending limit stops {title}. "
                "Ask to raise or remove this experiment's limit, then ask to run it again.")}
    raised = -(-max(cap * 2, spent + reserved + (ceiling_micro or 0)) // MICRO_PER_USD) * MICRO_PER_USD
    action = get_action("experiment.spend_cap")
    raise_card = replace(ConfirmationCard.for_action(action, interpretation=f"Change your saved limit for {title}.",
        consequences=[f"Your saved limit for {title} changes. No new run starts until you ask for it.",
                      "Run it again when you are ready. Nothing runs until you do."],
        resulting_state={"experiment_id": experiment_id, "cap_micro": raised}), confirm_label="Change experiment limit")
    stop = replace(ConfirmationCard.for_action(action, interpretation=f"Stop {title}.",
        consequences=["No new runs will start. Work already in progress may still finish. Your saved results are kept."],
        resulting_state={"experiment_id": experiment_id, "stop": True}), confirm_label="Stop it").as_action()
    stop["style"] = "secondary"
    return {"kind": "confirmation", "citations": [], "actions": [raise_card.as_action(), stop],
            "body_md": founder_copy(CAP_MESSAGE.format(title=title))}


def record_run(context: dict, run_id: str) -> None:
    """Keep every run id so lifetime spend survives retries (the context only holds the latest)."""
    if enabled():
        context["spend_run_ids"] = sorted(_run_ids(context) | {run_id})
