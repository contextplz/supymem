"""One saved "Idea" page per venture: the current idea for the founder and for Charlie.

Rewritten from saved venture state by one cheap metered call, at most once per
WINDOW_S per venture (a debounced background job). Experiments and outreach
counts come straight from saved rows; the model only condenses saved state.
"""
from __future__ import annotations

import contextvars
import json
import logging
import os
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.db.models import GtmTouch, Venture, VentureDocument
from app.core.local_mode import is_local
from app.state import db
from core.clock import as_utc, utcnow
from app.gtm.relationships.email_compliance import NO_DASH_RULE
from app.idea.validation_agent import _plain_strings
from app.idea.agent import summary_without_unsure

KIND = "idea_page"
JOB_KIND = "idea.page_refresh"
WINDOW_S = 300
UNKNOWN = "unknown"
# Finished jobs whose results can change the idea page.
REFRESH_ON_JOBS = frozenset({"idea.clarify_turn", "idea.validate", "research.deep", "gtm.god.run",
                             "gtm.journey.prepare", "experiment.evaluate", "pagebuilder.publish"})
_TEXT = ("summary", "problem", "offer")
_LISTS = ("open_questions", "what_changed")
log = logging.getLogger(__name__)


def _doc_id(venture_id: str) -> str:
    return f"{KIND}:{venture_id}"


def schedule(session, workspace_id: str | None, venture_id: str | None, *, actor: str | None = None,
             now: datetime | None = None, requested: bool = False) -> bool:
    """Queue a trailing refresh inside the caller's transaction; show Retry if enqueue fails."""
    if is_local() and not requested:
        return False
    if not is_local() and os.getenv("IDEA_PAGE_REFRESH", "on").strip().lower() == "off":
        return False
    from app.tenancy import current_or_none
    from app.worker.handlers import hosted_mode
    scope = current_or_none()
    actor = actor or (scope.actor if scope and scope.workspace_id == workspace_id else None)
    if not workspace_id or not venture_id or not actor or (hosted_mode() and actor in {"system", "anonymous", "local", "founder"}):
        return False
    from app.ventures.lifecycle import is_paused
    if is_paused(venture_id):
        return False
    from app.core.jobs import enqueue
    now = as_utc(now or utcnow())
    bucket = int(now.timestamp()) // WINDOW_S
    run_after = datetime.fromtimestamp((bucket + 1) * WINDOW_S, tz=timezone.utc)

    def queue():
        with session.begin_nested():
            enqueue(session, JOB_KIND, {"_actor_subject": actor, "_requested": requested}, workspace_id=workspace_id, venture_id=venture_id,
                    run_after=run_after, dedupe_key=f"{KIND}:{venture_id}:{bucket}" + (":requested" if is_local() else ""), max_attempts=1)

    try:
        # A fresh context: housekeeping, not an effect of the chat request, action receipt or task in flight.
        contextvars.Context().run(queue)
        return True
    except Exception as error:  # noqa: BLE001 - the idea page must never fail the founder's action
        _schedule_failed(venture_id, error, session=session)
        return False


def enqueue_failure_reply() -> dict:
    return {"kind": "action_failed",
            "body_md": "Your idea and answers are saved, but I could not update your Idea page. "
                "Select Retry here in chat. If it fails again, close and reopen the app, then select Retry.",
            "actions": [{"label": "Retry", "action": JOB_KIND, "retry": True, "payload": {}}], "citations": []}


def _schedule_failed(venture_id: str, error: Exception, *, session=None) -> None:
    from contextlib import nullcontext
    from app.chat.recovery import post_failure

    log.warning("idea_page_schedule_failed", extra={"error_class": type(error).__name__})
    try:
        reply = enqueue_failure_reply()
        with session.begin_nested() if session is not None else nullcontext():
            post_failure(venture_id, step="chat.action", kind="action_failed",
                key=f"idea-page-enqueue:{venture_id}:{int(utcnow().timestamp()) // WINDOW_S}",
                code="idea_page_enqueue_failed", session=session, requested=True,
                body_md=reply["body_md"], actions=reply["actions"])
    except Exception as notice_error:  # noqa: BLE001
        log.warning("idea_page_failure_notice_unavailable", extra={"error_class": type(notice_error).__name__})


def request_refresh(venture_id: str | None, *, actor: str | None = None, requested: bool = False) -> bool:
    """Schedule from code that holds no session (e.g. after a chat turn). Never raises."""
    if not venture_id:
        return False
    try:
        with db()() as session:
            venture = session.get(Venture, venture_id)
            if venture is None:
                return False
            scheduled = schedule(session, venture.workspace_id, venture_id, actor=actor, requested=requested)
            session.commit()
            return scheduled
    except Exception as error:  # noqa: BLE001
        _schedule_failed(venture_id, error)
        return False


def read(venture_id: str) -> dict | None:
    with db()() as session:
        row = session.get(VentureDocument, _doc_id(venture_id))
        if row is None or row.venture_id != venture_id or row.kind != KIND:
            return None
        try:
            page = json.loads(row.body_md or "{}")
        except ValueError:
            return None
        if not isinstance(page, dict):
            return None
        page = _plain_strings(page)
        if isinstance(page.get("summary"), str):
            page["summary"] = summary_without_unsure(page["summary"])
        evidence = _experiment_evidence(venture_id)
        if isinstance(page.get("experiments"), list):
            venture = session.get(Venture, venture_id)
            live = _plain_strings(experiments(venture_id, venture.workspace_id, evidence)) if venture else []
            names = {entry["name"] for entry in live}
            history = [_historical_experiment(entry) for entry in page["experiments"] if isinstance(entry, dict)
                       and isinstance(entry.get("name"), str) and entry["name"] not in names]
            page["experiments"] = history + live
        known = known_results(venture_id, evidence)
        if known or "what_we_know" in page:
            page["what_we_know"] = known
        return {"page": page, "updated_at": as_utc(row.updated_at).isoformat()}


def _experiment_evidence(venture_id: str) -> list[dict]:
    from app.experiments.ledger import evidence_for_venture

    try:
        return evidence_for_venture(venture_id)
    except Exception:  # noqa: BLE001 - an unavailable ledger must not hide the saved idea
        return []


def known_results(venture_id: str, rows: list[dict]) -> list[dict[str, str]]:
    """Grounded feedback, even if the optional model refresh has not run."""
    return [{"text": _plain_strings(f"{_OUTCOMES.get(row.get('outcome'), 'Outcome not recorded')}: {row['hypothesis']} | "
                                   f"{row.get('learning') or row.get('result_text') or 'No readable result recorded.'}"),
             "href": f"/v/{venture_id}/experiments?experiment_id={row['id']}"}
            for row in rows if row.get("state") == "concluded"]


def read_for(venture_id: str, subject: str | None) -> dict:
    """Founder API read: identity and workspace membership required (404 otherwise)."""
    from fastapi import HTTPException

    from app.authz.workspace import assert_venture_access
    if not subject or subject.strip() in {"", "anonymous", "system"}:
        raise HTTPException(401, detail="Sign in to open your company.")
    assert_venture_access(venture_id, subject)
    saved = read(venture_id) or {"page": None, "updated_at": None}
    return {"venture_id": venture_id, **saved}


def context_line(venture_id: str, limit: int = 700) -> str:
    """Compact line for Charlie's venture brief; empty when nothing is saved."""
    saved = read(venture_id)
    if not saved:
        return ""
    page = saved["page"]
    customers = page.get("customers") or {}
    parts = [f"summary: {page.get('summary') or UNKNOWN}",
             f"customers: {', '.join(customers.get('current') or []) or UNKNOWN}",
             f"problem: {page.get('problem') or UNKNOWN}", f"offer: {page.get('offer') or UNKNOWN}"]
    if page.get("open_questions"):
        parts.append("open questions: " + "; ".join(page["open_questions"][:3]))
    text = " ".join(f"Current idea page (updated {saved['updated_at'][:16]}): {' | '.join(parts)}".split())
    return text[:limit]


_EXPERIMENT_STATES = {"proposed": "Planned", "planned": "Planned", "running": "Testing", "concluded": "Complete",
                      "aborted": "Stopped", "stopped": "Stopped", "cancelled": "Stopped", "paused": "Paused",
                      "waiting_data": "Waiting for results"}
_EXPERIMENT_SOURCES = {"charlie_generated": "Suggested by Charlie", "validation": "Your validation review",
                       "research": "Your research review", "outreach": "Your outreach", "Sample": "Sample"}
_OUTCOMES = {"supported": "Worked", "refuted": "Didn't work", "inconclusive": "Too early to tell"}


def _historical_experiment(entry):
    status, source = str(entry.get("status") or ""), str(entry.get("source") or "")
    result = entry["result"] if isinstance(entry.get("result"), str) and entry["result"] else UNKNOWN
    outcome, sep, notes = result.partition(": ")
    if sep and outcome in _OUTCOMES:
        result = f"{_OUTCOMES[outcome]}: {notes}"
    return {"name": entry["name"], "result": result,
            "source": source if source in _EXPERIMENT_SOURCES.values() else _EXPERIMENT_SOURCES.get(source, "Saved experiment"),
            "status": status if status in _EXPERIMENT_STATES.values() else _EXPERIMENT_STATES.get(status, "Status not recorded")}


def experiments(venture_id: str, workspace_id: str | None, evidence: list[dict]) -> list[dict]:
    """Saved experiments and outreach results, read from rows (never from the model)."""
    from app.experiments.ledger import list_for_venture
    out = []
    try:
        rows = list_for_venture(venture_id)
    except Exception:  # noqa: BLE001
        rows = []
    rows = [*rows, *({**row, "source": row["spec"]["source"]} for row in evidence
                    if row.get("runner") == "system" and row["spec"].get("source") == "charlie_generated")]
    for row in rows[-8:]:
        result = row.get("result_text") or row.get("learning") or ""
        outcome = _OUTCOMES.get(row.get("outcome"))
        out.append({"name": row["hypothesis"][:200], "source": _EXPERIMENT_SOURCES.get(row.get("source"), "Saved experiment"),
                    "status": _EXPERIMENT_STATES.get(row["state"], "Status not recorded"),
                    "result": (f"{outcome}: {result}" if outcome and result else outcome or result)[:300] or UNKNOWN})
    with db()() as session:
        counts = dict(session.execute(select(GtmTouch.direction, func.count()).where(
            GtmTouch.workspace_id == workspace_id, GtmTouch.venture_id == venture_id, GtmTouch.simulated == 0,
        ).group_by(GtmTouch.direction)).all())
    if counts:
        sent, replies = counts.get("out", 0), counts.get("in", 0)
        out.append({"name": "Customer outreach", "source": "Your outreach", "status": "Activity recorded",
                    "result": f"{sent} sent, {replies} {'reply' if replies == 1 else 'replies'}"})
    return out


def _clean(value, limit: int) -> str:
    text = " ".join(str(value or "").split())[:limit]
    return text if text and text.casefold() not in {"none", "n/a", "null", "unknown"} else UNKNOWN


def _list(value, limit: int = 6) -> list[str]:
    items = value if isinstance(value, list) else []
    return [text for text in (_clean(item, 240) for item in items) if text != UNKNOWN][:limit]


def normalize(raw: dict, experiments_rows: list[dict]) -> dict:
    """Coerce model output to the page shape; missing or empty facts become 'unknown'."""
    raw = raw if isinstance(raw, dict) else {}
    customers = raw.get("customers") if isinstance(raw.get("customers"), dict) else {}
    page = {key: _clean(raw.get(key), 600 if key == "summary" else 400) for key in _TEXT}
    page["summary"] = summary_without_unsure(page["summary"])
    page["customers"] = {"current": _list(customers.get("current")), "alternatives": _list(customers.get("alternatives"))}
    page.update({key: _list(raw.get(key)) for key in _LISTS})
    from app.entry.chat_start import _GTM
    from app.ventures.startup_review import problem_is_request
    if _GTM.search(page["problem"]) and problem_is_request(page["problem"]):
        page["problem"] = UNKNOWN
        page["open_questions"] = list(dict.fromkeys([*page["open_questions"], "What problem does your product solve for your customers?"]))[:6]
    page["experiments"] = experiments_rows
    return page


PROMPT = ("You maintain a venture's Idea page. Rewrite it from the SAVED STATE only. Return JSON only: "
          '{"summary": <2-3 short plain sentences>, "customers": {"current": [<target customers / ICPs>], '
          '"alternatives": [<other customer segments the state mentions>]}, "problem": <one or two sentences>, '
          '"offer": <what is being offered now>, "open_questions": [<up to 5 unresolved questions>], '
          '"what_changed": [<up to 4 short notes on how the state differs from the previous page; [] if no previous page>]}. '
          'Never invent customers, results, numbers or decisions. Anything the state does not say is "unknown" '
          'Your goal (desired_outcome), such as acquiring customers or revenue, is not the customer problem. '
          'Use saved website problem and audience assumptions when available; otherwise leave them open. '
          'Unsure answers (idk, not sure, no idea, dunno) mean the point is still open, never quote them as facts. '
          '(or [] for lists). The latest founder statement wins over older text. Address the reader as "you" '
          'and "your", never "the founder". ' + NO_DASH_RULE)


async def refresh(payload: dict) -> None:
    """Worker handler: rewrite the page from saved state. Keeps the old page on any failure."""
    if is_local() and payload.get("_requested") is not True:
        return
    venture_id = payload.get("venture_id")
    from app.ventures.lifecycle import is_paused
    if venture_id and is_paused(venture_id):
        return
    with db()() as session:
        venture = session.get(Venture, venture_id) if venture_id else None
        if venture is None or venture.archived_at is not None:
            return
        workspace_id = venture.workspace_id
    from app.ventures.state_brief import get_venture_state_brief, render_brief_for_llm
    brief = get_venture_state_brief(venture_id, force=True)
    state = render_brief_for_llm({key: value for key, value in brief.items() if key != "idea_page"})
    rows = experiments(venture_id, workspace_id, _experiment_evidence(venture_id))
    previous = (read(venture_id) or {}).get("page")
    try:
        from app.credits import metered
        response = await metered.llm([
            {"role": "system", "content": PROMPT},
            {"role": "user", "content": json.dumps({"saved_state": state, "experiments": rows, "previous_page": previous},
                                                   ensure_ascii=False)}],
            purpose="venture.chat.assistant", workspace_id=workspace_id, venture_id=venture_id, tier="flash",
            max_tokens=3000)
        text = str(response.choices[0].message.content or "")
        raw = json.loads(text[text.index("{"):text.rindex("}") + 1])
    except Exception as error:  # noqa: BLE001 - an unusable reply keeps the last good page
        log.info("idea_page_refresh_skipped", extra={"error_class": type(error).__name__})
        return
    page = normalize(raw, rows)
    known = brief.get("idea") or {}
    from app.ventures.startup_review import problem_is_request
    problem = _clean(known.get("problem"), 400)
    if page["problem"] == UNKNOWN and problem != UNKNOWN and not problem_is_request(problem):
        page["problem"] = problem
        page["open_questions"] = [question for question in page["open_questions"]
                                  if question != "What problem does your product solve for your customers?"]
    if not page["customers"]["current"]:
        page["customers"]["current"] = _list([known.get("audience")])
    save(venture_id, page)


def save(venture_id: str, page: dict) -> None:
    with db()() as session:
        row = session.get(VentureDocument, _doc_id(venture_id))
        now = utcnow()
        if row is None:
            row = VentureDocument(id=_doc_id(venture_id), venture_id=venture_id, kind=KIND, title="Idea", created_at=now)
            session.add(row)
        row.body_md, row.updated_at = json.dumps(page, ensure_ascii=False), now
        session.commit()
    from app.ventures.state_brief import invalidate_venture_state_brief
    invalidate_venture_state_brief(venture_id)
