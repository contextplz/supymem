"""The PMF loop: God mode's way of iterating toward product-market fit after the first launch.

Each iteration is one riskiest assumption, one cheap change, one measurable signal read from real data
(waitlist sign-ups, outbound replies, interviews done), and a keep / pivot / kill decision. Several
run in parallel on different variables (positioning, who we talk to, channel, offer). Every iteration
is a row in the PMF journal (venture_documents, kind pmf_iteration), shown in the cockpit and chat.

States:  running -> keep | pivot | kill       (waiting_send is running with the clock paused)
Rules (decide()): the target reached is a keep; before the wait window ends nothing else is decided; at the end of it, some movement is a pivot (same assumption, new variant) and none is a kill (next assumption).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Awaitable, Callable

from sqlalchemy import func, select

from app.autopilot import claims, llm
from app.db.models import VentureDocument
from app.state import db
from core.clock import utcnow
from core.ids import new_id

KIND = "pmf_iteration"
MAX_PARALLEL = 3  # iterations running at once
MAX_STARTS_PER_TICK = 2
MAX_STARTS_PER_DAY = 6
MAX_HOURS = 72  # ponytail: one window for every metric; per-metric windows once real traffic volumes are known


@dataclass(frozen=True)
class Variable:
    name: str
    metric: str  # waitlist_signups | outbound_replies
    delta: int  # how much the metric must grow to call it a win
    needs_send: bool
    action: str
    variants: tuple[str, ...]  # the change, most promising first; {idea} is the venture's idea


VARIABLES = (
    Variable("positioning", "waitlist_signups", 5, False, "site.rewrite", (
        "Rewrite the headline and subhead to lead with the single most painful problem of {idea}, in the customer's own words.",
        "Rewrite the page around the concrete outcome the customer gets from {idea} in the first week.",
        "Rewrite the page to contrast {idea} against the way people cope today.")),
    Variable("icp", "outbound_replies", 2, True, "outreach.draft", (
        "Draft short outreach to the narrowest group who feel the problem behind {idea} most often.",
        "Draft short outreach to the group that already pays someone to solve the problem behind {idea}.",
        "Draft short outreach to people who recently complained about this problem in public.")),
    Variable("channel", "outbound_replies", 2, True, "outreach.draft", (
        "Draft a post for the online community where the customers of {idea} already gather.",
        "Draft a short direct message variant for {idea} that asks for a 15 minute conversation.",
        "Draft a referral ask to the first people who showed interest in {idea}.")),
    Variable("offer", "waitlist_signups", 5, False, "site.rewrite", (
        "Rewrite the call to action and offer so early access for {idea} is concrete and time-bound.",
        "Rewrite the page to offer a founder-led pilot of {idea} to the first ten people.")),
)
BY_NAME = {v.name: v for v in VARIABLES}


# ------------------------------------------------------------------ rules

def decide(baseline: float, target: float, measured: float, hours: float, *, max_hours: float = MAX_HOURS) -> str | None:
    """keep | pivot | kill, or None to keep measuring."""
    if measured >= target:
        return "keep"
    if hours < max_hours:
        return None
    return "pivot" if measured > baseline else "kill"


# ------------------------------------------------------------------ metrics from real data

def read_metric(venture_id: str, metric: str) -> int:
    """Real counts only: consented waitlist entries, inbound replies, outbound touches. Never invented."""
    from app.db.models import GtmContact, GtmTouch, Venture

    with db()() as s:
        ven = s.get(Venture, venture_id)
        ws = ven.workspace_id if ven else None
    if not ws:
        return 0
    if metric == "waitlist_signups":
        from app.conversion.waitlist import status

        return int(status(venture_id, ws)["total"])
    direction = "in" if metric == "outbound_replies" else "out"
    with db()() as s:
        return int(s.scalar(select(func.count(GtmTouch.id)).join(GtmContact, GtmContact.id == GtmTouch.contact_id)
                            .where(GtmContact.workspace_id == ws, GtmTouch.workspace_id == ws,
                                   GtmTouch.venture_id == venture_id, GtmTouch.simulated == 0,
                                   GtmTouch.direction == direction, GtmTouch.sent_at.is_not(None),
                                   (GtmTouch.classification.is_(None) | (GtmTouch.classification != "automated"))
                                   if direction == "in" else True)) or 0)


def log_touch(venture_id: str, workspace_id: str, who: str, direction: str, *, channel: str = "email",
              subject: str = "", note: str = "", logged_by: str | None = None) -> str:
    """Record outreach the founder sent by hand ("out") or a reply they got ("in"), so the PMF loop can
    count it. `who` is an email, handle or name. Real events only: callers report what actually happened."""
    from app.db.models import GtmContact, GtmTouch, Venture

    if direction not in ("out", "in"):
        raise ValueError("direction_invalid")
    who = who.strip().lower()[:200]
    if not who:
        raise ValueError("who_required")
    now = utcnow()
    with db()() as s:
        venture = s.get(Venture, venture_id)
        if venture is None or venture.workspace_id != workspace_id:
            raise ValueError("venture_scope_mismatch")
        c = s.scalar(select(GtmContact).where(GtmContact.workspace_id == workspace_id, GtmContact.email == who))
        if c is None:
            c = GtmContact(id=new_id("gtmc"), workspace_id=workspace_id, email=who, lawful_basis="none",
                           source="founder_manual", stage_changed_at=now, created_at=now)
            s.add(c)
            s.flush()
        if direction == "in":
            c.replied_at = now
        else:
            c.last_emailed_at, c.times_emailed = now, (c.times_emailed or 0) + 1
        t = GtmTouch(id=new_id("gtmtch"), workspace_id=workspace_id, venture_id=venture_id, contact_id=c.id, direction=direction,
                     channel=channel[:40], subject=subject[:300] or None, body_text=note[:2000] or None, sent_at=now,
                     simulated=0, send_mode="founder_manual", logged_by=logged_by, created_at=now)
        s.add(t)
        s.commit()
        return t.id


# ------------------------------------------------------------------ the journal

def _metric_scope(venture_id: str) -> dict[str, str]:
    from app.db.models import Venture
    with db()() as session:
        venture = session.get(Venture, venture_id)
        if venture is None:
            raise ValueError("venture_scope_unavailable")
        return {"version": "venture_human_reply_v2", "venture_id": venture_id, "workspace_id": venture.workspace_id}


def _metric_qualified(it: dict, venture_id: str, scope: dict | None = None) -> bool:
    return it.get("metric") == "waitlist_signups" or it.get("metric_scope") == (scope or _metric_scope(venture_id))


def _row(r: VentureDocument) -> dict[str, Any]:
    return json.loads(r.provenance_json or "{}") | {"doc_id": r.id}


def journal(venture_id: str, limit: int = 50) -> list[dict[str, Any]]:
    with db()() as s:
        rows = s.scalars(select(VentureDocument).where(VentureDocument.venture_id == venture_id,
                         VentureDocument.kind == KIND).order_by(VentureDocument.created_at.desc()).limit(limit)).all()
    if not rows:
        return []
    scope = _metric_scope(venture_id)
    values = [_row(r) for r in rows]
    return [it | {"metric_qualified": _metric_qualified(it, venture_id, scope)} for it in values]


def _save(venture_id: str, it: dict[str, Any], doc_id: str | None = None) -> str:
    now = utcnow()
    with db()() as s:
        row = s.get(VentureDocument, doc_id) if doc_id else None
        body = {k: v for k, v in it.items() if k != "doc_id"}
        if row is None:
            row = VentureDocument(id=new_id("pmf"), venture_id=venture_id, kind=KIND,
                                  title=f"PMF iteration {body['n']}: {body['variable']}", created_at=now, updated_at=now)
            s.add(row)
        row.provenance_json = json.dumps(body, default=str)
        row.body_md = f"{body['hypothesis']} -> {body.get('decision') or body['state']}"
        row.updated_at = now
        s.commit()
        return row.id


def _hours_since(iso: str) -> float:
    return (utcnow().replace(tzinfo=None) - datetime.fromisoformat(iso).replace(tzinfo=None)) / timedelta(hours=1)


# ------------------------------------------------------------------ hypotheses from the brief, not the raw idea string

_WHAT = {"positioning": "headline and subhead of the landing page", "icp": "narrower group of people to contact",
         "channel": "different place or format to reach people", "offer": "call to action and offer on the landing page"}
_HYPOTHESIS_SCHEMA = {"type": "object", "additionalProperties": False, "required": ["change"], "properties": {"change": {"type": "string"}}}


async def variant_text(venture_id: str, v: Variable, k: int, idea: str, history: list[dict[str, Any]]) -> str:
    """One concrete, testable change for this variable, written from the venture brief and research (DeepSeek V4 Pro).
    Falls back to the template variant when the model is unavailable or its text would claim things that do not exist."""
    template = v.variants[k].format(idea=idea[:80] or "your product")
    tried = [i["change"] for i in history if i["variable"] == v.name]
    out = await llm.ask_json(
        venture_id, "pmf.hypothesis",
        "You are a product-market-fit coach for a pre-revenue founder. Write ONE specific, testable change, grounded in the "
        "brief's real audience, problem and research. Never paste the founder's raw idea sentence. " + claims.HONESTY_RULES +
        " Reply with one JSON object.",
        f"{llm.brief_text(venture_id)}\n\nVariable to change: {v.name} ({_WHAT.get(v.name, v.name)}).\nDirection: {template}\n"
        f"Already tried, do not repeat: {json.dumps(tried)}\nReturn {{\"change\": \"one or two sentences\"}}.", _HYPOTHESIS_SCHEMA, 500)
    text = str((out or {}).get("change") or "").strip()
    return text if 20 <= len(text) <= 400 and not claims.violations(text, founder_words=claims.founder_words(venture_id)) else template


def accepted_build(venture_id: str) -> tuple[str | None, bool]:
    """(the newest accepted build of the venture's page, whether it could be published right now)."""
    from app.chat.site_actions import publish_preflight

    try:
        _, build_id, blockers = publish_preflight(venture_id)
    except Exception:  # noqa: BLE001
        return None, False
    return build_id, not blockers


# ------------------------------------------------------------------ choosing the next iterations

def next_candidates(venture_id: str, idea: str, history: list[dict[str, Any]], limit: int) -> list[tuple[Variable, int]]:
    """Cheapest untried (variable, variant) pairs, skipping variables already running so runs stay parallel.
    A pivot moves that variable to its next variant; a kill moves on to the next variable's first variant."""
    running = {i["variable"] for i in history if i["state"] in {"running", "needs_review"} or i.get("metric_qualified") is False}
    # One live test per metric: two tests moving the same count (sign-ups, replies) cannot be told apart, and both would claim the win.
    busy = {i["metric"] for i in history if i["state"] in {"running", "needs_review"}}
    out: list[tuple[Variable, int]] = []
    for v in VARIABLES:
        if v.name in running or v.metric in busy:
            continue
        used = {i["variant"] for i in history if i["variable"] == v.name}
        won = any(i.get("decision") == "keep" for i in history if i["variable"] == v.name)
        nxt = next((k for k in range(len(v.variants)) if k not in used), None)
        if nxt is None or (won and used):  # a kept change is the new baseline; stop poking that variable
            continue
        out.append((v, nxt))
    return out[:limit]


RunAction = Callable[[str, dict[str, Any]], Awaitable[bool]]


async def advance(venture_id: str, run_action: RunAction, *, allow_send: bool, idea: str, live: bool = False) -> list[dict[str, Any]]:
    """Measure and decide the running iterations, then start new ones. Returns the journal rows that changed.
    run_action(name, payload) runs one guarded autopilot action and says whether it went through."""
    changed: list[dict[str, Any]] = []
    history = journal(venture_id)
    for it in [i for i in history if i["state"] == "running"]:
        v = BY_NAME[it["variable"]]
        if not _metric_qualified(it, venture_id):
            # Preserve old measured/baseline values; do not compare fresh scoped
            # counts with historical workspace-wide totals or replay the action.
            it.update(state="needs_review", metric_qualified=False, metric_review_reason="legacy_unattributed_baseline")
            _save(venture_id, it, it["doc_id"])
            changed.append(it)
            continue
        if it.get("waiting_publish"):  # a live page changes only through a built, QA-passed, accepted build, then a guarded publish
            build_id, ready = accepted_build(venture_id)
            if build_id and build_id != it.get("base_build") and ready and await run_action("site.publish", {}):
                it.update(waiting_publish=False, started_at=utcnow().isoformat())  # the page is public: the clock starts now
            elif _hours_since(it["started_at"]) >= 2 * MAX_HOURS:
                it.update(state="kill", decision="kill", decided_at=utcnow().isoformat(), waiting_publish=False)
                changed.append(it)
            _save(venture_id, it, it["doc_id"])
            continue
        if v.needs_send and not allow_send and read_metric(venture_id, "outbound_touches") <= it.get("touches0", 0):
            it["waiting_send"] = True  # drafts exist; nothing was sent, so there is nothing to measure yet
            if _hours_since(it["started_at"]) >= 2 * MAX_HOURS:  # nobody sent anything in six days: stop holding the slot
                it.update(state="skip", decision="skip", decided_at=utcnow().isoformat(), waiting_send=False)
                changed.append(it)
            _save(venture_id, it, it["doc_id"])
            continue
        if it.get("waiting_send"):  # the founder sent them (or sending got allowed): the clock starts now
            it.update(waiting_send=False, started_at=utcnow().isoformat())
        now_value = read_metric(venture_id, v.metric)
        verdict = decide(it["baseline"], it["target"], now_value, _hours_since(it["started_at"]))
        it["measured"] = now_value
        if verdict:
            it.update(state=verdict, decision=verdict, decided_at=utcnow().isoformat())
            changed.append(it)
        _save(venture_id, it, it["doc_id"])
    history = journal(venture_id)
    running = sum(1 for i in history if i["state"] == "running" and not i.get("waiting_send"))  # a test waiting for the founder to send holds no slot
    today = sum(1 for i in history if i["started_at"][:10] == utcnow().date().isoformat())
    room = min(MAX_PARALLEL - running, MAX_STARTS_PER_TICK, MAX_STARTS_PER_DAY - today)
    started = 0
    # Walk every untried variable, not only the first `room`: one that cannot start today (no new people to write to, a publish
    # blocked) must not stop the others from running.
    for v, k in next_candidates(venture_id, idea, history, len(VARIABLES)):
        if started >= max(0, room):
            break
        change = await variant_text(venture_id, v, k, idea, history)
        live_page = live and v.action == "site.rewrite"
        names = ("build.prepare",) if live_page else ("site.rewrite", "site.publish") if v.action == "site.rewrite" else (v.action,)
        payload = ({"hint": f"Landing page copy change: {change} {claims.HONESTY_RULES}"} if live_page else
                   {"instruction": f"{change} {claims.HONESTY_RULES}"} if v.action == "site.rewrite" else
                   {"run": True, "batch": True, "recruitment_request": change})
        ok = True
        for name in names:
            ok = ok and await run_action(name, payload if name in (v.action, "build.prepare") else {})
        if not ok:
            continue  # blocked by a floor (cap, sending) or failed: the tick already logged why; try again later
        base = read_metric(venture_id, v.metric)
        it = {"n": len(history) + 1, "variable": v.name, "variant": k, "metric": v.metric,
              "hypothesis": f"Riskiest assumption ({v.name}): {change}", "change": change, "baseline": base,
              "target": base + v.delta, "measured": base, "metric_scope": _metric_scope(venture_id), "state": "running", "decision": None,
              "started_at": utcnow().isoformat(), "touches0": read_metric(venture_id, "outbound_touches") if v.needs_send else 0,
              "waiting_send": False, "waiting_publish": live_page, "base_build": accepted_build(venture_id)[0] if live_page else None}
        it["doc_id"] = _save(venture_id, it)
        history.append(it)
        changed.append(it)
        started += 1
        break  # the next candidate is chosen again next pass, once this test's metric is known to be busy
    return changed


def describe(it: dict[str, Any]) -> str:
    """One journal line: hypothesis, change, metric before/after, decision."""
    if it.get("metric_qualified") is False:
        return f"PMF iteration {it.get('n')}: historical measurement is unqualified; review the venture attribution before deciding."
    after = f"{it.get('baseline')} -> {it.get('measured')} (target {it.get('target')})"
    return (f"#{it['n']} {it['variable']}: {it['change']} | {it['metric'].replace('_', ' ')} {after} | "
            f"{'SKIPPED (nothing was sent)' if it.get('decision') == 'skip' else (it.get('decision') or 'running').upper()}")
