"""Refusal copy (27 §6.3)."""

from __future__ import annotations

from app.chat.context import ChatContext
from app.onboarding.state import AUTONOMY_WORDS


def refuse_unknown(ctx: ChatContext, phase_label: str = "setup finishes") -> dict:
    return {
        "kind": "refusal",
        "body_md": f"That is not recorded yet ({phase_label}). You can continue with available context; missing facts stay unknown.",
        "actions": [{"label": "Choose your next step", "action": "next_moves"}],
    }


def refuse_autonomy(ctx: ChatContext, required: str = "L4") -> dict:
    level = AUTONOMY_WORDS.get(ctx.governance.get("autonomy", "L1"), "Suggest")
    req = AUTONOMY_WORDS.get(required, required)
    return {
        "kind": "refusal",
        "body_md": f"That needs {req}. You're at {level}. Want me to raise it?",
        "actions": [{"name": "autonomy.raise", "label": f"Raise to {req}"}],
    }


def _recorded_facts(venture_id: str | None, workspace_id: str | None) -> list[str]:
    """What is actually recorded: ledger revenue and managed-waitlist requests (never invented)."""
    facts: list[str] = []
    if not venture_id:
        return facts
    try:
        from app.money.summary import money_summary

        money = money_summary(venture_id)
        if money.get("gross_received_minor") is None:
            facts.append("No verified live customer payment is recorded here; net earnings are unconfirmed")
        else:
            facts.append(f"Gross verified live customer payments: {money['gross_received_minor'] / (10 ** money['minor_unit_exponent']):.2f} {money.get('currency') or ''}. "
                         "Refunds, fees, net earnings and bank payouts are not reconciled.")
    except Exception as exc:  # noqa: BLE001 - grounding must never break chat
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, venture_id, "refuse.revenue")
    if workspace_id:
        try:
            from app.conversion.waitlist import status

            n = int(status(venture_id, workspace_id)["total"])
            facts.append(f"The waitlist has {n} consented sign-up{'' if n == 1 else 's'} (unverified emails, not proof of "
                         "demand)")
        except Exception as exc:  # noqa: BLE001
            from app.chat.recovery import log_founder_exception
            log_founder_exception(exc, venture_id, "refuse.waitlist")
        try:
            from app.state import db
            from app.ventures.state_brief import _analytics_section

            with db()() as session:
                seen = _analytics_section(session, venture_id, workspace_id)
            if seen.get("state") == "recorded":
                facts.append("Recorded browser events: " + ", ".join(
                    f"{n} {k.replace('_', ' ')}" for k, n in (seen.get("counts") or {}).items() if n)
                    + " (unverified events, not unique people)")
            elif seen.get("state") == "empty":
                facts.append("No visitor events are recorded yet")
        except Exception as exc:  # noqa: BLE001
            from app.chat.recovery import log_founder_exception
            log_founder_exception(exc, venture_id, "refuse.analytics")
    return facts


def refuse_revenue(ctx: ChatContext) -> dict:
    venture = ctx.venture or {}
    facts = _recorded_facts(venture.get("id"), venture.get("workspace_id")) + [f.rstrip(".") for f in ctx.limits[:2]]
    return {
        "kind": "refusal",
        "body_md": "I won't invent a number. Here's what we actually know: "
                   + ("; ".join(facts) if facts else "nothing connected yet") + ".",
        "actions": [],
    }
