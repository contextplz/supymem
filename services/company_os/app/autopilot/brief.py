"""God mode as a cofounder's status line: what I'm doing now, what is next, the one thing I need from you, what I learned.

One honest read of the saved state for the console strip and the daily note. It never claims more than the timeline
proves ("completed" = a saved effect was read back; "queued" = started, still running), hides internal bookkeeping
(admission rows, waits, retries) and names things in plain words rather than action codes.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from app.autopilot import guard, lifecycle, pmf
from app.credits.units import MICRO_PER_CREDIT
from core.clock import utcnow

DID = {
    "idea.answer": "Answered a question about your idea for you (marked as my assumption)",
    "idea.advance": "Validated your idea and confirmed it",
    "research.start": "Started deep research",
    "plan.generate": "Wrote a first plan",
    "plan.approve": "Approved the plan",
    "build.start": "Started building your landing page",
    "waitlist.enable": "Turned on your waitlist",
    "waitlist.test": "Tested the waitlist form",
    "site.publish": "Published your landing page",
    "outreach.targets": "Found people to talk to",
    "outreach.draft": "Wrote outreach drafts (nothing is sent without you)",
    "outreach.send": "Sent outreach from your Gmail",
    "mac.approve": "Approved a read-only Mac task",
    "content.drafts": "Wrote post drafts",
    "site.rewrite": "Rewrote the landing page copy (a draft, not public)",
    "job.retry": "Retried a step that failed",
}
# Rows that are bookkeeping, not news: the founder never needs to read them.
_QUIET = ("wait:", "human:")
_QUIET_OUTCOMES = {"attempt", "prepared", "info"}


def _plain(e: dict[str, Any]) -> tuple[str, str] | None:
    """(kind, sentence) for a timeline row worth showing, else None. kind: did | started | stopped | asked | heads_up"""
    action, outcome, what = e["action"], e["outcome"], e["what"]
    if action.startswith(_QUIET) or outcome in _QUIET_OUTCOMES and not action.startswith("god_mode"):
        return None
    if action == "pmf.iterate" or (what == f"{action}: {outcome}" and action not in DID):  # bookkeeping row: the real news has its own row
        return None
    if action in {"god_mode.on", "god_mode.off"}:
        return ("heads_up", what + ".")
    if action.startswith("pmf.") or action == "outreach.reply":
        return ("did" if outcome in {"completed", "done"} else "started", what)
    if outcome == "completed":  # a saved effect was read back
        return ("did", DID.get(action) or what)
    if outcome == "done":  # older rows: recorded, never verified: not claimed as finished
        return ("started", DID.get(action) or what)
    if outcome == "queued":  # admitted and running in the background
        return ("started", DID.get(action) or what)
    if outcome == "stopped":
        return ("stopped", what)
    if outcome == "blocked":
        return ("asked", e["what"])
    if outcome in {"held", "failed"}:
        return ("heads_up", f"I could not confirm that {DID.get(action, action).lower()} went through. Saved work is untouched.")
    return None


def recent(events: list[dict[str, Any]], limit: int = 8) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for e in events:  # newest first
        got = _plain(e)
        if got and (not out or out[-1]["text"] != got[1]):
            out.append({"at": e["at"], "kind": got[0], "text": got[1]})
        if len(out) >= limit:
            break
    return out


def learned(venture_id: str) -> list[str]:
    lines = []
    for it in pmf.journal(venture_id)[:10]:
        if it.get("decision") in {"keep", "pivot", "kill", "skip"}:
            after = f"{it.get('baseline')} to {it.get('measured')} {str(it.get('metric', '')).replace('_', ' ')} (needed {it.get('target')})"
            verdict = {"keep": "worked, keeping it", "pivot": "moved the numbers but not enough, trying a new angle",
                       "kill": "did not move anything, dropping it", "skip": "never ran because nothing was sent"}[it["decision"]]
            lines.append(f"Test #{it.get('n')} ({it.get('variable')}): {verdict}. {after}.")
    return lines[:3]


def _asks(venture_id: str) -> list[dict[str, str]]:
    from app.needs_you.service import list_open

    return [{"title": r["title"], "why": r["what_we_need"], "how": r["how_to_resume"]} for r in list_open(venture_id)]


def brief(venture_id: str, events: list[dict[str, Any]], observed: dict[str, Any] | None = None) -> dict[str, Any]:
    """Current activity comes from durable execution state; next moves are proposals."""
    from app.ventures.lifecycle import is_paused

    s = guard.get_settings(venture_id)
    asks = _asks(venture_id)
    out: dict[str, Any] = {"recent": recent(events), "learned": learned(venture_id), "waiting_on_you": asks[:1],
                           "more_waiting": max(0, len(asks) - 1), "now": "", "now_detail": "", "next": []}
    if not s["god_mode"]:
        return out | {"now": "God mode is off."}
    if is_paused(venture_id):
        return out | {"now": "Paused. Nothing new starts until you resume."}
    if observed is None:
        from app.autopilot.service import status

        observed = status(venture_id)
    # A planned branch is not evidence that a worker started it. Reuse the
    # same snapshot as the API status strip so the two surfaces cannot disagree.
    out["now"] = str(observed.get("working_on") or "Current activity could not be confirmed.")
    if observed.get("state") == "off":
        out["now"] = "God mode is off."
    try:
        primary = lifecycle.primary_move(venture_id)
        branches = lifecycle.branches(venture_id)
    except Exception:  # noqa: BLE001 - a status line never breaks the screen
        return out
    side = [b for b in branches if b.action]
    if primary.human:
        out["now_detail"] = f"Blocked on: {primary.title}." if primary.title else ""
    nxt = ([] if primary.human or primary.wait else [primary.title]) + [b.title for b in side]
    out["next"] = list(dict.fromkeys(nxt))[:2]
    if not out["next"] and primary.wait:
        out["next"] = ["Carry on as soon as that finishes."]
    return out


def daily_note(venture_id: str, rows: list[dict[str, Any]]) -> str:
    """The evening message: Did / Next / Waiting on you / Learned, short."""
    since = (utcnow() - timedelta(hours=24)).isoformat()
    b = brief(venture_id, [e for e in rows if e["at"] >= since])
    did = list(dict.fromkeys(r["text"] for r in b["recent"] if r["kind"] == "did"))[::-1][:6]
    going = [t for t in dict.fromkeys(r["text"] for r in b["recent"] if r["kind"] == "started") if t not in did][::-1][:4]
    s = guard.get_settings(venture_id)
    lines = ["**Today with God mode**"]
    lines.append("**Did:** " + ("; ".join(did) + "." if did else "nothing new finished today."))
    if going:
        lines.append("**Started today:** " + "; ".join(going) + ". These activity records do not confirm that work is still running.")
    if b["next"]:
        lines.append("**Next:** " + "; ".join(x.rstrip(".") for x in b["next"]) + ".")
    if b["waiting_on_you"]:
        w = b["waiting_on_you"][0]
        extra = f" (and {b['more_waiting']} more)" if b["more_waiting"] else ""
        lines.append(f"**Waiting on you:** {w['title']}{extra}. {w['why']}".strip())
    if b["learned"]:
        lines.append("**Learned:** " + " ".join(b["learned"]))
    lines.append(f"Used {s['spent_today_micro'] / MICRO_PER_CREDIT:g} credits today.")
    return "\n\n".join(lines)
