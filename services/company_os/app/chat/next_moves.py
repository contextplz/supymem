"""One saved-state, executable next-moves card for replies and return visits."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
from contextlib import nullcontext

from sqlalchemy import func, select

from app.db.models import ChatConversation, ChatMessage, GtmEmailEvent, GtmTouch, Job, Venture
from app.state import db
from app.chat.recovery import log_founder_exception

KIND = "next_moves"
STATUS_KINDS = {"entry_pending", "idea_clarification_pending", "idea_recovery", "status", "browse_progress",
                "gtm_search_progress", "gtm_diagnosis_pending", "deep_answer_pending"}
log = logging.getLogger(__name__)
_CAPABILITY_HEADING = "Other steps you can ask me to take:"
_COACHING_HEADING = "Here's the one thing only you can do:"
_OPEN_QUESTIONS_TEXT = "I'd like to share my open questions."
_CAPABILITY_REQUEST = re.compile(
    r"\b(?:what(?:['’]s|\s+is)?\s+next|what\s+(?:to\s+do|(?:should|can|could|do)\s+i\s+do)\s+next|"
    r"what\s+(?:can|could|will)\s+you\s+(?:do|help)|how\s+can\s+you\s+help|"
    r"what\s+(?:are\s+your\s+capabilities|do\s+you\s+offer))\b", re.I)
_REPLY_MOVES = (
    ("Continue clarifying", "idea.review", {}, r"\b(?:clarif\w*|answer\s+(?:the\s+)?(?:next\s+)?question)\b"),
    ("Start research", "research.prepare", {}, r"\bresearch\b"),
    ("Design an experiment", "experiment.start", {}, r"\b(?:design|run|start|plan)\s+(?:an?\s+)?experiment\b"),
    ("Find customers", "gtm.find", {"prepare": True}, r"\bfind\s+(?:potential\s+|your\s+first\s+)?customers\b"),
    ("Pressure-test your idea", "ceo_review", {}, r"\bpressure[ -]test\b"),
    ("See your next action", "whats_next", {}, r"\b(?:choose|see|show)\s+your\s+next\s+action\b"),
)
_PROPOSAL = re.compile(
    r"^(?:(?:the|your|our)\s+)?next step\b|"
    r"^(?:i\s+recommend|i\s+suggest|let['’]s|shall we|should we|can we|wanna|"
    r"(?:do you )?want (?:me )?to|would you like|would you prefer|would you rather|"
    r"(?:are you )?ready to|we (?:should|could)|i (?:can|could)(?!\s+report\b))\b|"
    r"^(?:Choose|Continue|Start|Design|Find|Pressure-test|Answer|Run|Review)\b", re.I)


def _retry_action(action: dict) -> bool:
    from app.chat.actions import REGISTRY

    action_name = action.get("action")
    registered = REGISTRY.get(action_name) if isinstance(action_name, str) else None
    if registered and registered.consequential and registered.name not in {"job.retry", "chat.model_retry"}:
        return False
    payload = action.get("payload")
    return (action.get("action") in {"job.retry", "chat.model_retry"} or action.get("retry") is True
            or isinstance(payload, dict) and payload.get("retry") is True)


def _question_action(action: dict) -> bool:
    payload = action.get("payload")
    question_id = payload.get("question_id") if isinstance(payload, dict) else None
    return action.get("action") == "chat.reply" and isinstance(question_id, str) and bool(question_id.strip())


def _prefill_action(action: dict) -> bool:
    payload = action.get("payload")
    text = payload.get("text") if isinstance(payload, dict) else action.get("text") if payload is None else None
    return action.get("action") == "chat.prefill" and isinstance(text, str) and bool(text.strip())


def _question_on_screen(session, venture_id: str, question_id: str) -> bool:
    from app.chat.conversations import current_conversation
    from app.chat.store import _partition

    scope = current_conversation()
    partition = _partition(scope) if scope else ChatMessage.conversation_id.is_(None) | ChatMessage.conversation_id.in_(
        select(ChatConversation.id).where(ChatConversation.venture_id == venture_id,
            ChatConversation.visibility == "legacy_shared"))
    return session.scalar(select(ChatMessage.id).where(ChatMessage.venture_id == venture_id,
        partition, ChatMessage.role == "assistant",
        ChatMessage.actions_json.contains(json.dumps({"question_id": question_id})[1:-1], autoescape=True)).limit(1)) is not None


def question_asked_last(session, venture_id: str, question_id: str) -> bool:
    """Charlie's newest message in this chat is this question, so a statement typed now answers it."""
    from app.chat.conversations import current_conversation
    from app.chat.store import _partition

    scope = current_conversation()
    partition = _partition(scope) if scope else ChatMessage.conversation_id.is_(None) | ChatMessage.conversation_id.in_(
        select(ChatConversation.id).where(ChatConversation.venture_id == venture_id,
            ChatConversation.visibility == "legacy_shared"))
    actions = session.scalar(select(ChatMessage.actions_json).where(ChatMessage.venture_id == venture_id,
        partition, ChatMessage.role == "assistant").order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).limit(1))
    return bool(actions) and json.dumps({"question_id": question_id})[1:-1] in actions


def intake_actions(card: dict, actions: list[dict]) -> list[dict]:
    from app.approvals import standing
    from app.chat.navigation_map import hidden_href
    from app.ventures import known
    actions = [action for action in actions if isinstance(action, dict)
               and isinstance(action.get("action", ""), str)
               and (action.get("confirmation") or action.get("action") not in card.get("blocked_actions", ()))]
    # Only the generic chip is dropped: a reply that names the idea as the next step keeps its way to do it.
    actions = [action for action in actions if action.get("action") != "idea.review" or action.get("label") != "Continue clarifying"
               or action.get("confirmation") or _retry_action(action)
               or any(move.get("action") == "idea.review" for move in card.get("actions") or [])]
    if not card.get("intake_pending"):
        return actions
    question_ready = any(_question_action(action) for action in actions)
    return [action for action in actions if action.get("confirmation") or _retry_action(action) or _prefill_action(action)
            or not (action.get("action") or action.get("name") or action.get("payload"))
                and isinstance(action.get("href"), str) and re.fullmatch(r"/(?!/)[^\\\x00-\x20]*", action["href"])
                and (action.get("kind") == "recovery_link" or hidden_href(action["href"]) is None)
            or action.get("action") == "chat.reply" and action.get("payload") == {"text": "Skip the questions"}
            or action.get("action") in {"gtm.status", "gtm.eligibility", "contacts.import", "startup.review",
                                        "jobs.status", "status"}
            or isinstance(payload := action.get("payload"), dict) and (
                isinstance(payload.get("search_context"), str) and bool(payload["search_context"].strip()) and (
                    action.get("action") == "contacts.preview"
                    or action.get("action") == "openworker_search" and isinstance(payload.get("query"), str)
                        and bool(payload["query"].strip()))
                or action.get("action") == "gtm.find" and payload.get("retry") is True and (
                    isinstance(payload.get("note"), str) and bool(payload["note"].strip())
                    or payload.get("recruitment_opt_in") is True))
            or action.get("action") in {"blocker.explain", "recovery.alternative", "recovery.submit", "autopilot.send_off",
                                        "idea.confirm", "idea.proposal.approve", "idea.proposal.edit", "referrals.show"}
            or _question_action(action)
            or known.enabled() and action.get("action") == "chat.reply" and action.get("known_choice") is True
            or standing.enabled() and action.get("action") in {"chat.stop", "approvals.revoke"}
            or not card.get("first_question_pending") and (
                action.get("action") == "idea.review" and not question_ready
                or action.get("action") == "chat.reply" and (
                    action.get("payload") == {"text": _OPEN_QUESTIONS_TEXT}))]


def align_reply(card: dict, body: str, actions: list[dict]) -> bool:
    """Align the card and reply actions; reconcile older invitations conservatively."""
    if card.get("stage") == "Paused" or not card.get("actions") or any(action.get("confirmation") for action in actions):
        return False
    from app.org.characters import parse_speaker

    _, body = parse_speaker(body)
    sentences = re.split(r"(?<=[.!?])\s+", body.strip())
    invitation = sentences[-1]
    if not _PROPOSAL.search(invitation):
        return False
    actions[:] = intake_actions(card, actions)
    available = [*(action for action in actions if action.get("action") != "chat.reply"), *card["actions"]]
    offered = {move.get("action") for move in available}
    available += [
        {"label": label, "action": action, "payload": payload}
        for label, action, payload, _ in _REPLY_MOVES
        if (not card.get("intake_pending") or action == "idea.review")
        and (action != "idea.review" or action in offered)
        and (action != "whats_next" or action in offered)
        and (action != "experiment.start" or action in offered or experiments_autopilot_enabled())]
    available += [action for action in actions if action.get("action") == "chat.reply" and not _question_action(action)]
    from app.chat.actions import disabled_reason
    available = [move for move in available if move.get("action") not in card.get("blocked_actions", ())
                 and not disabled_reason(move.get("action")) and not move.get("disabled_reason")]

    def choice(text):
        if re.search(r"\b(?:not|never|don't|do not|won't|cannot|can't)\b", text, re.I):
            return None
        explicit = [(match.start(), action) for action in available
                    if isinstance(action.get("label"), str) and action["label"]
                    and (match := re.search(r"\b" + re.escape(action["label"]) + r"\b", text, re.I))]
        selected = min(explicit, key=lambda item: item[0])[1] if explicit else None
        if selected and selected["action"] != "chat.reply":
            return selected
        if selected:
            text = selected["label"]
        if re.search(r"\b(?:report that|already (?:finished|completed|started|ran))\b|"
                     r"\b(?:research|experiment)\b.*\b(?:complete|completed|finished|saved)\b", text, re.I):
            return None
        # shortcut: legacy prose uses bounded invitation matching, prefer structured action labels for new replies.
        if re.search(r"\b(?:tell|share|discuss|list)\b.*\b(?:open questions|questions|unsure)\b", text, re.I):
            return {"label": "Share my open questions", "action": "chat.reply",
                    "payload": {"text": _OPEN_QUESTIONS_TEXT}}
        matches = [(match.start(), move)
                   for label, action, payload, pattern in _REPLY_MOVES
                   if (match := re.search(pattern, text, re.I))
                   and (move := next((item for item in available if item.get("action") == action), None))]
        return min(matches, key=lambda item: item[0])[1] if matches else selected

    proposed = []
    for text in re.split(r",?\s+or\s+", invitation, flags=re.I):
        move = choice(text)
        if move is None and len(sentences) > 1 and re.search(
                r"\b(?:jump into that|do that|start that|start it|get started|jump in)\b", text, re.I):
            previous = sentences[-2]
            if _PROPOSAL.search(previous):
                move = choice(previous)
        if move and not any((item["action"], item.get("payload", {})) ==
                            (move["action"], move.get("payload", {})) for item in proposed):
            proposed.append(move)
    if not proposed:
        return False
    remaining = [move for move in card["actions"] if not any(
        (move["action"], move.get("payload", {})) == (item["action"], item.get("payload", {}))
        for item in proposed)]
    card["actions"] = [{**move, "style": "primary" if index == 0 else "secondary",
                        "state_hash": card["state_hash"], **({"reply_choice": True} if index < len(proposed) else {})}
                       for index, move in enumerate([*proposed, *remaining])]
    original = [move for move in actions if not any(
        (move.get("action"), move.get("payload", {})) == (item["action"], item.get("payload", {}))
        for item in proposed)]
    actions[:] = [*card["actions"][:len(proposed)], *({**move, "style": "secondary"} for move in original)]
    if card.get("body_md"):
        card["body_md"] = re.sub(r"I recommend:[^\n]*",
            "I recommend: choose " + proposed[0]["label"] + " to take your next step.", card["body_md"], count=1)
        card["body_md"] = re.sub(r"Choose [^\n]* to take your next step\.$",
                                  "Choose " + proposed[0]["label"] + " to take your next step.", card["body_md"])
    return True


def experiments_autopilot_enabled() -> bool:
    from app.core.local_mode import is_local

    if is_local():
        from app.core.local_capabilities import enabled

        return enabled("run_for_me_experiments")
    from app.credits.policy import get_policy

    flags = get_policy("feature_flags")
    return isinstance(flags, dict) and flags.get("experiments_autopilot_v1") is True


def experiment_learning(venture_id: str) -> dict | None:
    """Read recorded learning and recommend within your current direction, without starting work."""
    from app.db.supymem_models import Experiment
    from app.experiments import beliefs, ledger
    from core.clock import as_utc, utcnow
    from core.copy import founder_copy

    with db()() as session:
        venture = session.get(Venture, venture_id)
        if not venture or venture.archived_at or venture.stage == "purged":
            return None
        from app.ventures.current_idea import intake_pending

        if intake_pending(venture_id, session=session):
            return None
        direction = ledger.current_direction(session, venture_id)
        saved = beliefs.row(session, venture_id)
        context = (json.loads(saved.context) if saved and saved.venture_id == venture_id
                   and saved.workspace_id == venture.workspace_id else {})
        scoped_ids = session.scalars(select(Experiment.id).where(
            Experiment.venture_id == venture_id, Experiment.workspace_id == venture.workspace_id,
            Experiment.kind == ledger.KIND).order_by(Experiment.started_at.desc().nulls_last(), Experiment.id.desc()))
        progress_order = {identifier: rank for rank, identifier in enumerate(scoped_ids)}
        if saved:
            progress_order.pop(saved.id, None)
    rows = [row for row in ledger.evidence_for_venture(venture_id) if row["id"] in progress_order]
    recorded = sorted((row for row in rows if row["state"] == "concluded"
        and row.get("outcome") in ledger.OUTCOMES and row.get("ended_at")
        and as_utc(row["ended_at"]) <= utcnow()), key=lambda row: (as_utc(row["ended_at"]), row["id"]))
    rows = [row for row in rows if row["direction"] == direction]
    results = [row for row in recorded if row["direction"] == direction]
    superseded = set()
    for belief in context.get("beliefs", []):
        linked = {item["experiment_id"] for item in belief.get("evidence", [])}
        history = [row["id"] for row in results if row["id"] in linked]
        superseded.update(history[:-1])
    results = [row for row in results if row["id"] not in superseded]
    running = min((row for row in rows if row["state"] == "running" and row.get("runner") != "system"
                   and row.get("ran") and row.get("result_text")), key=lambda row: progress_order[row["id"]], default=None)
    if not results and not running and not experiments_autopilot_enabled():
        return None
    supported = [row for row in results if row["outcome"] == "supported"]
    refuted = [row for row in results if row["outcome"] == "refuted"]
    plan = context.get("plan") or {}
    decision = None
    open_tests = [row for row in rows if row["state"] in {"proposed", "running"}
                  and (row.get("spec") or row.get("runner") != "system")]
    pivot = bool(refuted and not supported and (not open_tests or len(refuted) >= ledger.FAIL_THRESHOLD))
    if results:
        result = refuted[-1] if pivot else results[-1]
        why = ("Your completed test did not work." if pivot else
               "Your completed test gave a useful signal." if result["outcome"] == "supported" else
               "Your test did not work, but other tests are still open." if result["outcome"] == "refuted" else
               "Your test is too early to judge.")
        if result.get("learning"):
            why += f" What you learned: {result['learning']}"
        else:
            why += f" Your test: {result['hypothesis']}"
        decision = {"id": ("pivot:" if pivot else "persevere:") + result["id"] + ":" + result["outcome"],
            "title": "Consider a pivot" if pivot else "Keep testing your direction", "why": why,
            "action": "experiment.review" if pivot else "experiment.list", "payload": {}}
    recommendation = decision
    if running:
        recommendation = {"id": "progress:" + running["id"], "title": "Finish your current test",
            "why": f"Your test is still running. {running.get('learning') or running['result_text']} "
                "Finish the remaining work or tell me you are ending the test, then report your final results.",
            "action": "experiment.list", "payload": {}}
    elif not pivot:
        queued = next((row for test in plan.get("tests", []) if test["state"] == "queued"
            for row in rows if row["id"] == test["experiment_id"] and row["state"] == "proposed"), None)
        if experiments_autopilot_enabled() and queued and plan.get("state") in {"draft", "active"}:
            recommendation = {"id": "test:" + queued["id"], "title": f"Test next: {queued['hypothesis']}",
                "why": "Your saved plan has this unanswered question ready to test; review it before starting.",
                "action": "experiment.recommendation.prepare", "payload": {"experiment_id": queued["id"]}}
        elif not results:
            proposed = next((row for row in rows if row["state"] == "proposed" and row.get("runner") != "system"), None)
            untested = next((item for item in context.get("beliefs", []) if item["status"] == "to_test"), None)
            if proposed and plan.get("state") != "stopped":
                recommendation = {"id": "test:" + proposed["id"], "title": f"Test next: {proposed['hypothesis']}",
                    "why": "You have no recorded result for this saved test yet; review it before starting.",
                    "action": "experiment.recommendation.prepare", "payload": {"experiment_id": proposed["id"]}}
            elif untested and plan.get("state") not in {"stopped", "budget_exhausted"}:
                recommendation = {"id": "plan:" + untested["id"], "title": f"Plan your next test: {untested['statement']}",
                    "why": "You have no recorded result for this saved idea yet; review a plan before starting.",
                    "action": "experiment.beliefs.prepare", "payload": {}}
    for item in (recommendation, decision):
        if item:
            item["title"] = founder_copy(" ".join(item["title"].split()))
            item["why"] = founder_copy(" ".join(item["why"].split()))
    return {"beliefs": context.get("beliefs", []), "plan": plan, "results": recorded,
            "recommendation": recommendation, "decision": decision}


def _fresh_recommendation(venture_id: str) -> dict | None:
    try:
        learning = experiment_learning(venture_id)
        recommendation = learning.get("recommendation") if learning else None
        if not recommendation:
            return None
        if recommendation["id"].startswith("progress:"):
            return recommendation
        with db()() as session:
            saved = session.scalar(select(ChatMessage.citations_json).where(
                ChatMessage.venture_id == venture_id, ChatMessage.role == "assistant",
                ChatMessage.citations_json.contains('"recommendation"', autoescape=True))
                .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).limit(1))
        for item in json.loads(saved or "[]"):
            previous = item.get("next_moves", {}).get("recommendation") or {}
            if previous.get("id") == recommendation["id"]:
                return None
        return recommendation
    except Exception as exc:
        log_founder_exception(exc, venture_id, "next_moves.learning")
        return None


def _journey_snapshot(workspace_id: str, venture_id: str) -> dict:
    from app.autopilot import outreach
    from app.gtm import journey, reachable
    from app.connections import mailboxes

    current = journey._icp(venture_id)
    with db()() as session:
        row = journey._row(session, venture_id)
        data = journey._json(row.provenance_json) if row else {}
        job = session.get(Job, data.get("job_id")) if data.get("job_id") else None
        touches = (GtmTouch.workspace_id == workspace_id, GtmTouch.venture_id == venture_id,
                   GtmTouch.simulated == 0)
        sent = session.scalar(select(func.count()).select_from(GtmTouch).where(
            *touches, GtmTouch.direction == "out", GtmTouch.sent_at.is_not(None))) or 0
        reply_counts = dict(session.execute(select(GtmTouch.classification, func.count()).where(
            *touches, GtmTouch.direction == "in").group_by(GtmTouch.classification)).all())
        replied = sum(count for classification, count in reply_counts.items() if classification != "automated")
        refs = mailboxes.list_read_mailboxes(session, workspace_id)
        connected = False
        project = os.environ.get("COMPOSIO_PROJECT", "").strip()
        for ref in refs:
            available = bool(not str(ref["connected_account_id"] or "").startswith("sim_")
                and outreach._EMAIL.fullmatch(ref["email"] or "") and ref["composio_user_id"] and ref["connected_account_id"])
            if available and project:
                marker = session.scalar(select(GtmEmailEvent.metadata_json).where(GtmEmailEvent.email_account_id == ref["mailbox_id"],
                    GtmEmailEvent.event_type == "composio_project")
                    .order_by(GtmEmailEvent.created_at.desc(), GtmEmailEvent.id.desc()).limit(1))
                available = journey._json(marker).get("project", "v1") == project
            connected = connected or available
    icp = (dict(data["icp"]) if data.get("icp") and journey.usable_problem(data["icp"].get("problem"))
           and data.get("state") in {"queued", "running", "failed"}
           else journey._journey_icp(venture_id, data.get("icp"), base=current))
    if current.get("problem_revision_id") and (data.get("icp") or {}).get("problem_revision_id") != current["problem_revision_id"]:
        icp = current
    loaded = {"contacts": {}, "companies": {}}
    prospects = journey._prospects(venture_id, wid=workspace_id, icp=icp, current=current, loaded=loaded)
    drafts = outreach.drafts(venture_id, limit=journey.STATUS_LIMIT,
        audience=str(icp.get("who") or "") + " " + journey._audience_location(str(icp.get("who") or ""), icp),
        people=loaded["contacts"], companies=loaded["companies"])
    drafts = [item for item in drafts if item.get("state") not in {"review_staging", "review_abandoned"}
              and not (item.get("suggestion_draft") and not item.get("contact_id"))
              and (not journey._consumer(icp) or item.get("suggestion_draft") is True)]
    leads = reachable.visible((data.get("reachable_leads") or []) + (data.get("industry_experts") or []),
                              workspace_id, b2b=not journey._consumer(icp), venture_id=venture_id)
    leads = [item for item in leads if journey.in_audience_geography(icp, item)
             and not item.get("expert_role") and item.get("lead_type") != "insight"]
    work = journey.contact_work.view(workspace_id, venture_id, limit=journey.STATUS_LIMIT)
    delivered = {item.get("contact_id") for item in drafts if item.get("state") == "sent"}
    sent += sum(bool(item.get("sent_at")) for item in work["manual_outreach"] if item.get("id") not in delivered)
    state = "failed" if job and job.status in {"failed_dead", "poison", "failed", "cancelled"} else data.get("state", "idle")
    return {"icp": icp, "state": state, "job_id": data.get("job_id"), "prospects": prospects,
            "reachable_leads": leads, "drafts": [{**item, "revision": journey._draft_revision(item)} for item in drafts],
            "results": {"sent": sent, "replied": replied}, "gmail": {"connected": connected}}


def _snapshot(venture_id: str) -> dict:
    from app.lead_library.service import match
    from app.needs_you.policy import HUMAN_ONLY
    from app.needs_you.service import list_open
    from app.ventures.next_step import _facts
    from app.ventures.state_brief import get_venture_state_brief
    from app.ventures.journey_overrides import chose_customers

    facts = _facts(venture_id)
    brief = get_venture_state_brief(venture_id, force=True)
    paused = bool(facts.get("paused") or (brief.get("idea") or {}).get("paused"))
    view = _journey_snapshot(facts["workspace_id"], venture_id)
    customers_chosen = chose_customers(venture_id)
    try:
        library = match(venture_id, icp=view["icp"])
    except Exception as exc:
        log_founder_exception(exc, venture_id, "next_moves.library")
        library = {}
    founder = next((item for item in list_open(venture_id) if item["reason_code"] in HUMAN_ONLY), None)
    from app.ventures.run_status import WORK
    from app.planning.store import list_open_tasks
    from app.db.supymem_models import IdeaSession, IdeaRevision, ClarifierTurn
    with db()() as session:
        jobs = list(session.execute(select(Job.id, Job.status).where(Job.venture_id == venture_id,
            Job.kind.in_([*WORK, "experiment.run"])).order_by(Job.id)).tuples())
        idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture_id,
            IdeaSession.workspace_id == facts["workspace_id"], IdeaSession.state != "abandoned")
            .order_by(IdeaSession.updated_at.desc(), IdeaSession.created_at.desc(), IdeaSession.id.desc()).limit(1))
        revision = session.get(IdeaRevision, idea.current_revision_id) if idea else None
        latest = session.scalar(select(ClarifierTurn).where(ClarifierTurn.session_id == idea.id)
            .order_by(ClarifierTurn.turn.desc()).limit(1)) if idea else None
        questions_skipped = bool(revision and json.loads(revision.structured or "{}").get("_questions_skipped"))
        review_ready = ({"session_id": idea.id, "revision_id": revision.id} if idea and revision
            and idea.state == "reviewing" and latest and latest.role == "agent" and latest.kind == "proposal" else None)
        reading_idea = bool(idea and latest and idea.state in {"open", "recon", "clarifying", "proposing", "validating"}
            and (latest.role == "founder" or latest.kind in {"opening", "proposal"}))
        open_question = bool(idea and latest and idea.state not in {"confirmed", "abandoned"}
            and latest.role == "agent" and latest.kind == "question")
        question_on_screen = open_question and _question_on_screen(session, venture_id, latest.id)
    from app.experiments.exploration import continue_selected

    return continue_selected(venture_id, {
        "idea": {**(brief.get("idea") or {}), "confirmed": facts.get("idea_confirmed", False)}, "icp": view["icp"],
        "idea_state": facts.get("idea_state"), "jobs": jobs,
        "plan_state": facts.get("plan_state") if facts.get("plan_current") else None,
        "plan_id": facts.get("plan_id") if facts.get("plan_current") else None,
        "first_question_pending": facts.get("first_question_pending", False),
        "review_ready": review_ready and not customers_chosen, "questions_skipped": questions_skipped,
        "reading_idea": reading_idea and not customers_chosen,
        "questions_pending": not customers_chosen and not questions_skipped and not question_on_screen and (
            open_question and not facts.get("idea_confirmed")
            or bool(facts.get("startup_review") and not facts["startup_review"]["complete"])),
        "customers_chosen": customers_chosen,
        "review": facts.get("startup_review"),
        "has_customers": facts.get("has_customers", False),
        "research": (brief.get("research") or {}) if facts.get("research_report_ready") else {},
        "research_running": facts.get("active_research", False),
        "search_state": view.get("state"), "search_job": view.get("job_id"),
        "search_request": view.get("discovery_note") or "",
        "leads": [item["id"] for item in view.get("prospects") or []],
        "social_leads": [item["id"] for item in view.get("prospects") or [] if item.get("linkedin_url") or item.get("x_handle")],
        "reachable": [item.get("id") for item in view.get("reachable_leads") or []],
        "drafts": [(item.get("doc_id"), item.get("state"), item.get("revision")) for item in view.get("drafts") or []],
        "sent": (view.get("results") or {}).get("sent", 0),
        "gmail_connected": (view.get("gmail") or {}).get("connected", False),
        "replied": (view.get("results") or {}).get("replied", 0),
        "page": bool(facts.get("landing") or facts.get("external_site")),
        "page_build": facts.get("page_build"), "published": facts.get("published_site_url"),
        "product": facts.get("accepted_product", False),
        "experiment": facts.get("exp_run"), "experiment_review": facts.get("exp_review"),
        "experiments": brief.get("experiments") or [],
        "library": [item["id"] for item in library.get("items") or []],
        "skipped": sorted((facts.get("journey_overrides") or {}).get("skipped") or {}),
        "paused": paused,
        "founder": {key: founder[key] for key in ("id", "reason_code", "what_we_need")} if founder else None,
        "priorities": [] if paused else [task["title"] for task in list_open_tasks(venture_id)
                       if task.get("source") == "startup_priorities"],
        "priority_step": brief.get("next_step") if (facts.get("startup_review") or {}).get("complete")
                         and not facts.get("personal") and not (facts.get("journey_overrides") or {}).get("target") else None,
    })


def _customer_calls_offer(venture_id: str, state: dict) -> dict | None:
    if not experiments_autopilot_enabled():
        return None
    from app.tasks import goals, service

    if state["search_state"] in {"queued", "running"} or not goals.enabled() or not (state["leads"] or state["reachable"]
            or state["icp"].get("confirmed") is True and str(state["icp"].get("who") or "").strip()):
        return None
    from app.chat.conversations import current_conversation
    from app.chat.store import _partition
    from app.db.supymem_models import Experiment
    from app.db.task_models import Task
    from app.experiments import ledger
    from app.experiments.outreach_tests import booked_reply, calendar_booked
    from core.clock import utcnow

    label = "Book 3 customer calls this week"
    with db()() as session:
        venture = session.get(Venture, venture_id)
        if not venture:
            return None
        scopes = session.scalars(select(Task.scope_json).where(Task.workspace_id == venture.workspace_id,
            Task.venture_id == venture_id, Task.state.not_in(service.TERMINAL)))
        for saved in scopes:
            scope = json.loads(saved or "{}")
            if scope.get("_goals_v1") or any(step.get("subgoal") for step in scope.get("steps", [])):
                return None
        marks = {row.id: json.loads(row.context).get("booking_marks", {}) for row in session.scalars(
            select(Experiment).where(Experiment.workspace_id == venture.workspace_id, Experiment.venture_id == venture_id,
                Experiment.kind == ledger.KIND, Experiment.context.contains('"booking_marks"', autoescape=True)))}
        if any(mark is True for contacts in marks.values() for mark in contacts.values()):
            return None
        metadata = [json.loads(saved or "{}") for saved in session.scalars(select(GtmEmailEvent.metadata_json).where(
            GtmEmailEvent.workspace_id == venture.workspace_id, GtmEmailEvent.event_type == "booking_reply_headers"))]
        booking_headers = {item.get("reply_id"): item for item in metadata}
        replies = session.scalars(select(GtmTouch).where(GtmTouch.workspace_id == venture.workspace_id,
            GtmTouch.venture_id == venture_id, GtmTouch.direction == "in", GtmTouch.simulated == 0,
            GtmTouch.created_at <= utcnow()))
        for reply in replies:
            facts = json.loads(reply.honesty_json or "{}")
            if marks.get(facts.get("experiment_id"), {}).get(reply.contact_id) is False:
                continue
            observed = booking_headers.get(reply.id, {})
            headers = observed.get("reply_headers") or facts.get("reply_headers")
            subject = reply.subject or observed.get("subject", "")
            if booked_reply(reply.body_text, headers=headers, subject=subject) and (
                    reply.classification != "automated" or calendar_booked(reply.body_text, headers, subject)):
                return None
        conversation = current_conversation() or session.scalar(select(ChatConversation).where(
            ChatConversation.venture_id == venture_id, ChatConversation.visibility == "legacy_shared"))
        partition = _partition(conversation) if conversation else ChatMessage.conversation_id.is_(None)
        previous = session.scalar(select(ChatMessage).where(ChatMessage.workspace_id == venture.workspace_id,
            ChatMessage.venture_id == venture_id, partition, ChatMessage.role == "assistant",
            (ChatMessage.kind == KIND) | ChatMessage.citations_json.contains('"next_moves"', autoescape=True))
            .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).limit(1))
        if previous:
            actions = json.loads(previous.actions_json or "[]")
            for item in json.loads(previous.citations_json or "[]"):
                actions += item.get("next_moves", {}).get("actions", [])
            if any(action.get("action") == "chat.reply" and str(
                    (action.get("payload") or {}).get("text") or action.get("text") or "").casefold() == label.casefold()
                    for action in actions):
                return None
    return {"label": label, "action": "chat.reply", "payload": {"text": label}}


def _blocked_actions(venture_id: str) -> set[str]:
    """Actions whose own handler would turn the founder away right now, by the handler's own check."""
    from app.chat.build_path import research_prerequisites
    from app.chat.experiments import GATED_ACTIONS, _idea_gate

    return ({"research.prepare"} if research_prerequisites(venture_id) else set()) | (
        set(GATED_ACTIONS) if _idea_gate(venture_id) else set())


def compose(venture_id: str, *, requested: bool = False) -> dict:
    from app.chat.site_actions import refresh_page_actions

    return refresh_page_actions([_compose(venture_id, requested=requested)], venture_id)[0]


def _compose(venture_id: str, *, requested: bool = False) -> dict:
    from app.chat.actions import disabled_reason
    from app.core.local_mode import is_local
    from app.ventures.stage import get_stage

    state = _snapshot(venture_id)
    try:
        founder_stage = get_stage(venture_id)
    except KeyError:  # archived or purged venture: keep the artifact-based label
        founder_stage = {"stage": "idea", "label": "Shaping your idea"}
    state = {**state, "founder_stage": founder_stage}
    page_ready = is_local() and not state["published"] and state["page_build"] == "ready"
    from app.chat.build_moves import recommendation as build_recommendation
    build_move = build_recommendation(venture_id, state) if is_local() and not state.get("page") and state.get("page_build") == "failed" else None
    priority = state.get("priority_step")
    if not build_move and not state["paused"] and not page_ready and priority and (priority.get("stage") == "Your priority"
            or priority.get("key") in {"research.start", "research.retry", "research.running"}):
        body = (f"{priority['title']}. {priority['detail']} No new work was started." if is_local() else
                f"I recommend: {priority['title']}. Why: {priority['detail']}")
        if state.get("priorities"):
            body += "\n\nYour next priorities:\n" + "\n".join(
                f"{index}. {title}" for index, title in enumerate(state["priorities"], 1))
        return {"kind": KIND, "stage": founder_stage["label"],
                "state_hash": hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest(),
                "body_md": body if is_local() else
                    body + f"\n\nChoose {priority['cta_label']} to take your next step. No new work was started.",
                "actions": [{"label": priority["cta_label"], "href": priority["href"], "style": "primary"}], "citations": []}
    build_move = build_move or build_recommendation(venture_id, state)
    from app.chat import plan_chain
    plan_state = state.get("plan_state")
    if not build_move and not state["paused"] and state.get("idea_state") == "confirmed" and plan_state in {"founder_review", "approved", "executing"}:
        task = plan_chain.next_task(venture_id) if plan_state in {"approved", "executing"} else None
        reply = (plan_chain._text(f"Your approved plan includes **{task['title']}** in Work. Open Work to review it and choose your next step.",
                    plan_chain._open("Open Work", f"/v/{venture_id}/tasks")) if task else
                 plan_chain._text("Your plan is saved. Open it to review the current draft and choose your next step.",
                    plan_chain._open("Review your plan", f"/v/{venture_id}/plan")))
        return {**reply, "stage": founder_stage["label"], "state_hash": hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()}
    if not state["paused"]:
        from app.chat.customers import _search_card, saved_search_request, search_note
        state["search_request"] = saved_search_request(venture_id) or ""
        if search_note(state["search_request"], state["icp"].get("who") or ""):
            state["library"] = []
    digest = hashlib.sha256(json.dumps(state, sort_keys=True, default=str).encode()).hexdigest()
    moves = []
    from app.db.supymem_models import Experiment
    from app.experiments import ledger

    completed_invitations = set()
    if state["drafts"]:
        with db()() as session:
            completed_invitations = {identifier for row in session.scalars(select(Experiment).where(
                Experiment.venture_id == venture_id, Experiment.kind == ledger.KIND, Experiment.state == "concluded"))
                for identifier in ledger._ctx(row).get("outreach_draft_ids") or []}
    state = {**state, "drafts": [draft for draft in state["drafts"] if draft[0] not in completed_invitations]}
    draft_count = sum(draft[1] == "draft" for draft in state["drafts"])

    setup_reason = None

    def offer(label, action, payload=None, href=None):
        nonlocal setup_reason
        if action in blocked:
            return False
        if action and (reason := disabled_reason(action)):
            setup_reason = setup_reason or reason
            return False
        if action == "ceo_review" and state.get("idea_state") in {"open", "recon", "clarifying", "validating", "proposing", "reviewing"}:
            return False
        if draft_count and action == "outreach.draft":
            return False
        if not any(move["action"] == action and move["payload"] == (payload or {}) for move in moves):
            moves.append({"label": label, "action": action, "payload": payload or {}, **({"href": href} if href else {})})
        return True

    if state["paused"]:
        return {"kind": KIND, "stage": "Paused", "state_hash": digest,
                "body_md": "Your venture is paused. Nothing new will start. Choose Resume when you want to continue.",
                "actions": [{"label": "Resume", "action": "resume", "style": "primary", "state_hash": digest}], "citations": []}
    if state.get("review_ready") and not build_move:
        from app.chat.execute import _review_idea
        return {**_review_idea(venture_id), "kind": KIND, "stage": state["founder_stage"]["label"],
                "state_hash": digest, "intake_pending": True, "review_ready": True}
    if requested:
        intake_context = f"Your saved idea: {state['idea'].get('idea_summary') or state['idea'].get('name') or 'your idea'}."
        audience = state["idea"].get("audience") or state["icp"].get("who")
        if audience:
            intake_context += f" Your audience: {audience}."
    if state.get("reading_idea") and not build_move:
        from app.chat.execute import _review_idea
        reply = _review_idea(venture_id)
        if reply["kind"] == "idea_recovery":  # nothing is running, so "I'm reading it" would be untrue however early this is
            return {**reply, "kind": KIND, "stage": state["founder_stage"]["label"], "state_hash": digest, "intake_pending": True}
        if reply["kind"] == "idea_clarification_pending":
            if state["founder"]:
                reply["body_md"] += f"\n\nHere's the one thing only you can do: {state['founder']['what_we_need']}"
                reply["actions"] = reply["citations"] = []  # a new message, not the job's saved started row
            if requested:
                reply["body_md"] = f"{intake_context}\n\n{reply['body_md']} Choose Check your idea review to see your saved progress."
                reply["actions"] = [{"label": "Check your idea review", "action": "status", "style": "primary"}]
            return {**reply, "kind": "status" if state.get("first_question_pending") and not requested else KIND,
                    "stage": state["founder_stage"]["label"], "state_hash": digest, "intake_pending": True,
                    "first_question_pending": state.get("first_question_pending", False)}
    if not build_move and not state["idea"].get("confirmed") and not state.get("customers_chosen") and (state.get("first_question_pending")
            or state.get("idea_state") in {"open", "recon", "clarifying", "reviewing", "validating"}
            and "clarification" not in state["skipped"]):
        first_question_pending = bool(state.get("first_question_pending"))
        questions_pending = state.get("questions_pending", True) and not first_question_pending
        body = ("Your idea is saved. I'm reading it now and will ask the one thing I most need to understand."
                if first_question_pending else "Answer the next question so I can understand your idea." if questions_pending
                else "Your answers are saved. Use the question or review already in your chat to continue.")
        if state["founder"]:
            body += f"\n\nHere's the one thing only you can do: {state['founder']['what_we_need']}"
        if requested:
            body = f"{intake_context}\n\n{body}"
            if not questions_pending:
                body += " Choose Check your idea review to see your saved progress."
        return {"kind": "status" if first_question_pending and not requested else KIND,
                "stage": state["founder_stage"]["label"], "state_hash": digest,
                "intake_pending": True, "first_question_pending": first_question_pending,
                "body_md": body,
                "actions": [{"label": "Check your idea review", "action": "status", "style": "primary"}]
                    if requested and not questions_pending else [] if not questions_pending else [{"label": "Continue clarifying", "action": "idea.review",
                    "payload": {}, "style": "primary", "state_hash": digest}], "citations": []}
    blocked = _blocked_actions(venture_id)
    questions_pending = state.get("questions_pending", not state["idea"].get("confirmed")) and "clarification" not in state["skipped"]
    recommendation = build_move or _fresh_recommendation(venture_id)
    if requested and recommendation is None:
        try:
            recommendation = (experiment_learning(venture_id) or {}).get("recommendation")
        except Exception as exc:
            log_founder_exception(exc, venture_id, "next_moves.learning")
    if build_move:
        offer(build_move["title"], build_move["action"], build_move["payload"], build_move.get("href"))
    if draft_count and not (recommendation and recommendation["id"].startswith("progress:")):
        offer(f"Review your draft{'s' if draft_count != 1 else ''}", "gtm.status", {"show_saved": True})
    if is_local() and state["icp"].get("needs_input") and not draft_count:
        offer("Choose your customer audience", "gtm.find", {"prepare": True})
    if recommendation:
        # The full recommendation and its reason are in the message body; the button stays a short action.
        if offer(recommendation["title"].split(":", 1)[0].strip(), recommendation["action"], recommendation["payload"]):
            digest = hashlib.sha256((digest + json.dumps(recommendation, sort_keys=True)).encode()).hexdigest()
        else:
            recommendation = None
    if draft_count:
        offer(f"Review your draft{'s' if draft_count != 1 else ''}", "gtm.status", {"show_saved": True})
    if (recommendation and recommendation["action"] in {"experiment.list", "experiment.review"}
            and completed_invitations and not draft_count):
        blocked |= {"outreach.draft", "gtm.automate"}
    goal_offer = _customer_calls_offer(venture_id, state)
    if goal_offer:
        offer(goal_offer["label"], goal_offer["action"], goal_offer["payload"])
        digest = hashlib.sha256((digest + json.dumps(goal_offer, sort_keys=True)).encode()).hexdigest()
    if state.get("exploration"):
        selected = state["exploration"]
        offer("Continue with your selected idea", "experiment.exploration.continue",
              {"round_id": selected["round_id"], "option_id": selected["option_id"]})
    stage = state["founder_stage"]["label"]
    if state["sent"]:
        focus = "Learning from outreach"
    elif state["drafts"]:
        focus = "Outreach drafts ready"
    elif state["leads"] or state["reachable"]:
        focus = "Customers found"
    elif state.get("has_customers"):
        focus = "Growing your customer base"
    elif state["page"] or state["published"]:
        focus = "Finding your first customers"
    elif state["research"].get("present"):
        focus = "Research ready"
    elif state["research_running"] or state["search_state"] in {"queued", "running"}:
        focus = "Researching your audience"
    else:
        focus = "Shaping your idea" if state["founder_stage"]["stage"] == "idea" else stage

    if page_ready:
        offer("Set up publishing and signups", "", href="/settings/local#publishing-title")
        offer("Find customers", "gtm.find", {"prepare": True})
        if state["research_running"] or state["research"].get("present"):
            offer("Review your research", "research")
        else:
            offer("Pressure-test with research", "research.prepare")

    experiment = state["experiment"]
    if experiment and experiment.get("state") == "proposed" and experiment.get("id"):
        from app.experiments import autonomous
        if not autonomous.run_blocked_reason(venture_id, experiment["id"]):
            offer(f"Run: {experiment.get('title') or experiment.get('hypothesis') or 'your saved test'}", "experiment.recommendation.prepare", {"experiment_id": experiment["id"]})
        else:
            offer("Review your tests", "experiment.list")
    elif (not experiment or experiment.get("state") == "proposed" and not experiment.get("id")) and experiments_autopilot_enabled():
        offer("Design an experiment", "experiment.start")
    if state["library"]:
        offer("Add library leads", "gtm.library_claim", {"entry_ids": state["library"]})
    if state["leads"] or state["reachable"]:
        offer("Draft emails", "outreach.draft", {"review_only": True})
    if state["drafts"] and not state["icp"].get("needs_input"):
        if state["gmail_connected"]:
            offer("Automate emails", "gtm.automate", {"prepare": True})
    if not state["research_running"] and not state["research"].get("present"):
        if state["idea"].get("confirmed") or state["skipped"]:
            offer("Pressure-test with research", "research.prepare")
        else:
            offer("Pressure-test your next move" if state["sent"] or state.get("has_customers") else "Pressure-test your idea", "ceo_review")
    if state["search_state"] not in {"queued", "running"}:
        if state["icp"].get("needs_input"):
            if is_local():
                offer("Choose your customer audience", "gtm.find", {"prepare": True})
            else:
                offer("Draft editable emails", "outreach.draft", {"review_only": True})
        else:
            note = _search_card(state)["payload"].get("note")
            offer("Find customers", "gtm.find", {"prepare": True, **({"note": note} if note else {})})
    from app.pagebuilder.api import enabled as pagebuilder_enabled
    if not is_local() and pagebuilder_enabled() and not state["page"] and state["page_build"] != "running":
        offer("Build your landing page", "pagebuilder.build", {"prepare": True})
    if state["social_leads"]:
        offer("Draft LinkedIn notes", "gtm.library_notes", {"contact_ids": state["social_leads"]})
    if len(moves) < 2:
        offer("Pressure-test your next move", "ceo_review")
        if not is_local() or not state["icp"].get("needs_input") and (state["leads"] or state["reachable"]):
            offer("Draft editable emails", "outreach.draft", {"review_only": True})
    moves = moves[:3]
    questions_pending = (state.get("questions_pending", not state["idea"].get("confirmed"))
                         and "clarification" not in state["skipped"])
    if questions_pending and not draft_count and not build_move:
        if not state["idea"].get("confirmed"):
            moves.insert(0, {"label": "Continue clarifying", "action": "idea.review", "payload": {}})
        else:  # idea.review only repeats "confirmed" here; the saved answers are what is left.
            moves.insert(0, {"label": "Review your answers here", "action": "startup.review", "payload": {}})
    if not moves:
        return {"kind": KIND, "stage": stage, "focus": focus, "state_hash": digest,
                "body_md": setup_reason if is_local() and setup_reason else
                    "Your saved progress is unchanged. Choose Show your saved status to review it and select your next step.",
                "actions": [{"label": "Keys and connections", "href": "/settings/local", "style": "primary"}]
                    if is_local() and setup_reason else
                    [{"label": "Show your saved status", "action": "status", "style": "primary"}], "citations": []}
    primary_recommendation = recommendation and moves[0]["action"] == recommendation["action"]
    if draft_count and moves[0]["action"] == "gtm.status":
        body = (f"Your draft{' is' if draft_count == 1 else 's are'} ready.\n\n"
                f"Choose {moves[0]['label']} to take your next step.")
    else:
        founder = state["founder"]
        only_you = ("Finish the remaining work or tell me you are ending the test, then report your final results."
                    if recommendation and recommendation["id"].startswith("progress:") else
                    founder["what_we_need"] if founder else
                    "Connect your sending account before any email automation." if state["drafts"] and not state["gmail_connected"] else
                    "Review your drafts and tell me which messages you want sent." if state["drafts"] else
                    "Talk to one potential customer and tell me what you learn.")
        body = "Your private page is ready. " if page_ready else f"You're at {stage}. "
        if primary_recommendation:
            body += (f"{recommendation['title']}. {recommendation['why']}\n\n" if is_local() else
                     f"I recommend: {recommendation['title']}. Why: {recommendation['why']}\n\n")
        else:
            why = {"": "Publishing lets people visit your page and sign up.",
                   "ceo_review": "Turn your saved idea into one concrete test, without more questions.",
                   "idea.review": "Answer one question to make your saved idea clearer.",
                   "startup.review": "Your saved answers are the step left before research and experiments.",
                   "research.prepare": "Check competitors and demand before you invest more.",
                   "gtm.find": ("Name your audience before looking for people you can interview." if is_local() and state["icp"].get("needs_input")
                                else "Test your named audience through real customer conversations."),
                   "outreach.draft": ("Review a message that fits the person before you decide whether to contact them." if is_local()
                                      else "Prepare messages you can review before anything is sent."),
                   "experiment.start": "Test your riskiest assumption before building more.",
                   "experiment.recommendation.prepare": "Your proposed test is ready for you to review."}.get(
                       moves[0]["action"], f"Move your saved progress ({focus.lower()}) into work you can review.")
            body += f"I recommend: choose {moves[0]['label']} to take your next step. Why: {why}"
            if recommendation:
                body += f" Your saved progress: {recommendation['title']}. {recommendation['why']}"
            body += "\n\n"
        if state.get("priorities"):
            body += "Your next priorities:\n" + "\n".join(
                f"{index}. {title}" for index, title in enumerate(state["priorities"], 1)) + "\n\n"
        statuses = {status for _, status in state.get("jobs", [])}
        if statuses & {"leased", "running"}:
            body += "Your work is underway. You can keep going with the options below.\n\n"
        elif statuses & {"queued", "pending", "scheduled", "retry_wait"}:
            body += "Your work is waiting to start. You can keep going with the options below.\n\n"
        alternatives = [move for move in (moves if primary_recommendation else moves[1:]) if not recommendation or
                        (move["action"], move["payload"]) != (recommendation["action"], recommendation["payload"])]
        if alternatives or not is_local():
            body += _CAPABILITY_HEADING + "\n\n" + "\n".join(
                f"- I can {move['label'][0].lower() + move['label'][1:]} for you now." for move in alternatives[:2])
        body += f"\n\nHere's the one thing only you can do: {only_you}"
        if questions_pending:
            body += "\n\nQuestions are optional. You can skip them and keep going with saved assumptions."
        body += f"\n\nChoose {moves[0]['label']} to take your next step."
    from core.copy import strip_emdash
    for index, move in enumerate(moves):
        move["style"] = "primary" if index == 0 else "secondary"
        move["state_hash"] = digest
    return {"kind": KIND, "stage": stage, "focus": focus, "state_hash": digest, "body_md": strip_emdash(body),
            "actions": moves, "citations": [], **({"recommendation": recommendation} if recommendation else {}),
            **({"blocked_actions": sorted(blocked)} if blocked else {})}


def questionnaire_reply(venture_id: str, body: str, request: str) -> dict | None:
    """Bind a model's intake follow-up to the open saved question, never to a new answer slot."""
    from app.chat.assistant import _ASKS_LIST_RE, _asks_about_last_result
    from app.chat.idea_bridge import written_request
    from app.chat.intent import _ANSWER_ONLY

    if written_request(request) or _ASKS_LIST_RE.search(request) or _ANSWER_ONLY.search(request) or _asks_about_last_result(request):
        return None
    # shortcut: English question punctuation identifies follow-ups, use structured reply intent for other languages.
    body = re.sub(r'"[^"]*"|“[^”]*”|(?<!\w)[\'‘].*?[\'’](?!\w)|`[^`]*`|^[ \t]*>[^\n]*', "", body, flags=re.S | re.M)
    questions = re.findall(r"(?:^|(?<=[.!?])\s+)[\s*#\d.)-]*"
        r"((?:who|what|which|when|where|why|how|can|could|do|does|is|are|have|will|would)\b[^?\n]*\?)", body, re.I | re.M)
    if not questions:
        return None
    card = compose(venture_id)
    if not card.get("intake_pending") or not any(not align_reply({**card}, question, []) for question in questions):
        return None
    from app.chat.execute import _review_idea
    reply = _review_idea(venture_id)
    return reply if reply["kind"] == "idea_clarification_ready" else None


def refused_work(body: str) -> bool:
    return bool(re.search(r"\b(?:i|you)\s+(?:cannot|can['’]t|won['’]t)\b", body or "", re.I))


def _without_coaching(body: str, *, reply: bool = False) -> str:
    if reply:
        before, heading, after = body.partition(_CAPABILITY_HEADING)
        if heading:
            body = before + after.lstrip("\n").partition("\n\n")[2]
        return body.strip().replace(_COACHING_HEADING + " ", "", 1)
    return "\n\n".join(paragraph for paragraph in body.split("\n\n") if not paragraph.startswith(_COACHING_HEADING))


def short_reply(body: str) -> str:
    from app.org.characters import parse_speaker, stamp_speaker

    if _COACHING_HEADING not in body and _CAPABILITY_HEADING not in body:
        return body
    speaker, prose = parse_speaker(body)
    return stamp_speaker(_without_coaching(prose, reply=True), speaker)


def _saved_recommendation(row: ChatMessage) -> tuple[str | None, str, list] | None:
    from app.chat.store import _LOCAL_RECOMMENDATION_STAGE

    metadata = json.loads(row.citations_json or "[]")
    actions = json.loads(row.actions_json or "[]")
    for item in metadata:
        if not isinstance(item, dict):
            continue
        if _LOCAL_RECOMMENDATION_STAGE in item:
            return item[_LOCAL_RECOMMENDATION_STAGE], row.body_md, actions
        card = item.get("next_moves")
        if isinstance(card, dict) and card.get("stage") and card.get("body_md"):
            return card["stage"], card["body_md"], card.get("actions") or actions
    if (_COACHING_HEADING in row.body_md or _CAPABILITY_HEADING in row.body_md
            or row.kind == KIND and actions and not any(_question_action(action) or action.get("confirmation") for action in actions)):
        stage = re.search(r"You're at ([^.]+)\.", row.body_md)
        return stage.group(1) if stage else None, row.body_md, actions
    return None


def _idle_history(session, scope):
    from datetime import timedelta

    from app.chat.requests import STALE_TURN_SECONDS
    from app.chat.store import _partition, _request_message
    from app.db.supymem_models import FounderRequest
    from app.ventures.run_status import ACTIVE, WORK
    from core.clock import utcnow

    messages = select(ChatMessage).where(ChatMessage.venture_id == scope.venture_id,
        _partition(scope), ChatMessage.role == "assistant").order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc())
    last = session.scalar(messages.limit(1))
    if last is None or last.kind in STATUS_KINDS - {"status", "idea_recovery"} or _saved_recommendation(last):
        return None
    actions = json.loads(last.actions_json or "[]")
    if last.body_md.rstrip().endswith("?") or any(_question_action(action) or action.get("confirmation") for action in actions):
        return None
    founder = _request_message(session, scope)
    if founder is not None and founder.created_at > last.created_at:
        return None
    if session.scalar(select(FounderRequest.id).where(FounderRequest.venture_id == scope.venture_id,
            FounderRequest.workspace_id == scope.workspace_id, FounderRequest.source == "chat_turn_v1",
            FounderRequest.outcome == "pending", FounderRequest.created_at >= utcnow() - timedelta(seconds=STALE_TURN_SECONDS),
            FounderRequest.text.contains('"conversation_id":' + json.dumps(scope.id), autoescape=True)).limit(1)):
        return None
    if session.scalar(select(Job.id).where(Job.venture_id == scope.venture_id,
            Job.kind.in_([*WORK, "experiment.run"]), Job.status.in_(ACTIVE)).limit(1)):
        return None
    return messages


def admit_local_recommendation(session, scope, row: ChatMessage, stage: str) -> bool:
    """Check and record the recommendation while holding the conversation write lock."""
    from app.chat.store import _LOCAL_RECOMMENDATION_STAGE

    history = _idle_history(session, scope)
    actions = json.loads(row.actions_json or "[]")
    venture = session.get(Venture, scope.venture_id)
    if (history is None or venture is None or venture.archived_at or not actions
            or any(_question_action(action) or action.get("confirmation") for action in actions)):
        return False

    def step(moves):
        primary = next((move for move in moves if move.get("style") == "primary"), moves[0] if moves else {})
        return (primary.get("action"), primary.get("href"), primary.get("payload") or {})

    previous = False
    for earlier in session.scalars(history.execution_options(yield_per=20)):
        saved = _saved_recommendation(earlier)
        if saved is None:
            continue
        previous_stage, previous_body, previous_actions = saved
        if not previous and previous_actions and step(actions) == step(previous_actions):
            return False
        if previous_stage is not None and stage != previous_stage:
            break
        if previous_stage == stage and _COACHING_HEADING in previous_body:
            row.body_md = _without_coaching(row.body_md)
            break
        previous = True
    metadata = json.loads(row.citations_json or "[]")
    row.citations_json = json.dumps([*metadata, {_LOCAL_RECOMMENDATION_STAGE: stage}])
    return True


def attach(venture_id: str, row: ChatMessage, *, session=None, scope=None, request: str = "") -> bool:
    """Keep next-move buttons on the reply; narrate capabilities only when useful."""
    if row.kind == "reminder":
        return False
    try:
        from app.chat.assistant import _asks_about_last_result
        from app.chat.idea_bridge import direct_request, written_request
        from app.chat.intent import LOOKUP_INTENTS, WORK_INTENTS, _ANSWER_ONLY, classify_intent
        from app.chat.store import _partition, _REPLY_TO
        from app.core.local_mode import is_local
        from app.ventures.run_status import ACTIVE, WORK
        metadata = json.loads(row.citations_json or "[]")
        actions = json.loads(row.actions_json or "[]")
        actions = [action for action in actions if isinstance(action, dict)
                   and isinstance(action.get("action", ""), str)] if isinstance(actions, list) else []
        if (row.kind in {"idea_clarification_ready", "question"} or any(_question_action(action) for action in actions)) and not any(
                isinstance(item, dict) and item.get("question") is True for item in metadata):
            metadata.append({"question": True})
        if row.kind == "idea_clarification_ready" or any(_question_action(action) for action in actions):
            row.actions_json = json.dumps(intake_actions({"intake_pending": True, "actions": []}, actions))
            row.citations_json = json.dumps([item for item in metadata if "next_moves" not in item])
            return False
        if any(isinstance(item, dict) and item.get("question") is True for item in metadata):
            row.actions_json = json.dumps(actions)
            row.citations_json = json.dumps([item for item in metadata if "next_moves" not in item])
            return False
        if row.kind in STATUS_KINDS:
            from app.approvals import standing
            from app.tasks import reasoning
            row.actions_json = json.dumps([action for action in actions if action.get("confirmation")
                or row.kind == "deep_answer_pending" and reasoning.enabled() and action.get("action") == "status"
                    and isinstance(action.get("payload"), dict) and isinstance(action["payload"].get("reasoning"), dict)
                or action.get("action") == "chat.reply" and action.get("payload") == {"text": "Skip the questions"}
                or standing.enabled() and action.get("action") in {"chat.stop", "approvals.revoke"}
                or _retry_action(action) or action.get("action") in {
                    "blocker.explain", "recovery.alternative", "recovery.submit", "autopilot.send_off"}])
            row.citations_json = json.dumps([item for item in metadata if "next_moves" not in item])
            return False
        if (row.kind in {"confirmation", "navigation", "refusal", "idea_correction_saved", "memory_list", "connections_status", "gtm_diagnosis_quote", "gtm_route_confirmed"}
                or any(action.get("confirmation") for action in actions)
                or any("next_moves" in item or "job_started" in item for item in metadata) or _ANSWER_ONLY.search(request)
                or written_request(request)
                or _asks_about_last_result(request)):
            return False
        asks_for_next_step = bool(_CAPABILITY_REQUEST.search(request))
        local = is_local()
        if local and not actions and not asks_for_next_step:
            return False
        card = compose(venture_id, requested=True) if asks_for_next_step else compose(venture_id)
        actions = intake_actions(card, actions)
        row.actions_json = json.dumps(actions) if actions else None
        if card.get("intake_pending"):
            actions = intake_actions(card, actions)
            row.actions_json = json.dumps(actions) if actions else None
            if not card.get("actions") or card.get("first_question_pending") or any(_question_action(action)
                    or action.get("action") in {"idea.proposal.approve", "idea.proposal.edit"} for action in actions):
                return False
        if refused_work(row.body_md) or (not asks_for_next_step and (direct_request(request)
                or classify_intent(request) in (LOOKUP_INTENTS | WORK_INTENTS) - {"assistant", "general", "status", "whats_next"})):
            return False
        if row.kind in {"text", "task_completed"} and align_reply(card, row.body_md, actions):
            row.actions_json = json.dumps(actions)
        if local:
            if asks_for_next_step and row.kind in {KIND, "text", "task_completed"} and not any(_retry_action(action) for action in actions):
                selected = (actions if row.kind == KIND else card["actions"])[:1]
                if selected:
                    row.actions_json = json.dumps(selected)
            return False
        if row.kind == KIND and actions and card.get("actions"):
            primary = next((action for action in actions if action.get("style") == "primary"), actions[0])
            if (any(primary.get(field) != card["actions"][0].get(field) for field in ("label", "action"))
                    and align_reply(card, f"Choose {primary.get('label', '')}.", actions)):
                row.body_md = card["body_md"]
                row.actions_json = json.dumps(actions)
        previous, previous_card = None, None
        with nullcontext(session) if session is not None else db()() as session:
            if not asks_for_next_step and session.scalar(select(Job.id).where(Job.venture_id == venture_id,
                    Job.kind.in_([*WORK, "experiment.run"]), Job.status.in_(ACTIVE)).limit(1)):
                return False
            conversation = scope or (session.get(ChatConversation, row.conversation_id) if row.conversation_id else
                session.scalar(select(ChatConversation).where(ChatConversation.venture_id == venture_id,
                    ChatConversation.visibility == "legacy_shared")))
            partition = _partition(conversation) if conversation else ChatMessage.conversation_id.is_(None)
            history = session.scalars(select(ChatMessage).where(ChatMessage.venture_id == venture_id,
                partition, ChatMessage.role == "assistant", ChatMessage.id != row.id)
                .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).execution_options(yield_per=20))
            for earlier in history:
                if previous is None:
                    previous = earlier
                previous_card = next((item["next_moves"] for item in json.loads(earlier.citations_json or "[]")
                                      if isinstance(item.get("next_moves"), dict)), None)
                if previous_card:
                    break
                if earlier.kind == KIND and (stage := re.search(r"You're at ([^.]+)\.", earlier.body_md)):
                    previous_card = {"stage": stage.group(1)}
                    break
            request_id = next((item[_REPLY_TO] for item in metadata if _REPLY_TO in item), None)
            if request_id is None:
                request_id = session.scalar(select(ChatMessage.id).where(ChatMessage.venture_id == venture_id,
                    partition, ChatMessage.role.in_(["user", "founder"]))
                    .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).limit(1))
            fresh_request = request_id is not None and request_id != (previous_card or {}).get("founder_message_id")
            if fresh_request and asks_for_next_step:
                fresh_request = not any(isinstance(item.get("next_moves"), dict)
                    and item["next_moves"].get("founder_message_id") == request_id
                    for saved in session.scalars(select(ChatMessage.citations_json).where(
                        ChatMessage.venture_id == venture_id, partition, ChatMessage.role == "assistant",
                        ChatMessage.id != row.id,
                        ChatMessage.citations_json.contains(json.dumps({_REPLY_TO: request_id}), autoescape=True)))
                    for item in json.loads(saved or "[]"))
        previous_block = bool(previous and (_CAPABILITY_HEADING in previous.body_md or any(
            _CAPABILITY_HEADING in str(item.get("next_moves", {}).get("body_md", ""))
            for item in json.loads(previous.citations_json or "[]"))))
        baseline = (previous_card or {}).get("capability_stage", (previous_card or {}).get("stage", card["stage"]))
        show = not previous_block and (asks_for_next_step and fresh_request
            or card["stage"] != baseline)
        card["capability_stage"] = card["stage"] if show else baseline
        card["founder_message_id"] = request_id
        if (row.kind in {KIND, "text", "task_completed"} and fresh_request
                and asks_for_next_step and not any(_retry_action(action) for action in actions)):
            selected = (actions if row.kind == KIND else card["actions"])[:1]
            if selected:
                if row.kind == KIND:
                    card["base_body_sha256"] = hashlib.sha256(row.body_md.encode()).hexdigest()
                    body = row.body_md.partition("\n\n")[0]
                    body = re.sub(rf"You're at {re.escape(card['stage'])}\.\s*", "", body, count=1)
                    row.body_md = body.rstrip() + f"\n\nChoose {selected[0]['label']} to take your next step."
                row.actions_json = json.dumps(selected)
                card["actions"], card["body_md"] = [], ""
                row.citations_json = json.dumps([*metadata, {"next_moves": card}])
                return True
        if row.kind == KIND and actions:
            card["actions"] = []
        if _CAPABILITY_HEADING in row.body_md:
            if not show:
                from app.chat.jarvis import classify
                card["base_body_sha256"] = hashlib.sha256(row.body_md.encode()).hexdigest()
                body, _, capabilities = row.body_md.partition(_CAPABILITY_HEADING)
                body = re.sub(rf"You're at {re.escape(card['stage'])}\.\s*", "", body, count=1)
                body = re.sub(r"Your work is (?:underway|waiting to start)\. "
                              r"You can keep going with the options below\.\s*", "", body, count=1)
                if classify(request) == "jarvis.blocked":
                    founder_heading = "Here's the one thing only you can do:"
                    founder = capabilities.partition(founder_heading)[2].partition("\n\nQuestions are optional.")[0]
                    body = body.rstrip() + "\n\n" + founder_heading + founder
                row.body_md = body.strip() or "Here are your next steps."  # never leave an empty bubble
            card["body_md"] = ""
        elif not show:
            card["body_md"] = ""
        row.citations_json = json.dumps([*metadata, {"next_moves": card}])
        return True
    except Exception as exc:
        log_founder_exception(exc, venture_id, "next_moves.attach")
        return False


def post(venture_id: str, *, conversation_id: str | None = None, requested: bool = False, message_id: str | None = None) -> bool:
    """Post an idle local recommendation, or preserve the hosted milestone attachment."""
    from app.chat.conversations import resolve
    from app.chat.store import _partition, _RID, _REPLY_TO, _request_message
    from app.core.local_mode import is_local
    from app.db.session import serialize_writes

    local = is_local()
    if local and (message_id or requested):
        return False
    try:
        scope = resolve(venture_id, conversation_id)
        if local:
            with db()() as session:
                venture = session.get(Venture, venture_id)
                if venture is None or venture.archived_at or _idle_history(session, scope) is None:
                    return False
            card = compose(venture_id)
            if card["kind"] != KIND or card.get("intake_pending") or not card.get("actions"):
                return False
            from app.chat.store import append_message

            return bool(append_message(venture_id, conversation_id=scope.id, role="assistant", kind=KIND,
                body_md=card["body_md"], actions=card["actions"], citations=card["citations"],
                recommendation_stage=card["stage"]))
        with db()() as session, session.no_autoflush:
            venture = session.get(Venture, venture_id)
            if venture is None or venture.archived_at:
                return False
            query = select(ChatMessage).where(ChatMessage.venture_id == venture_id,
                _partition(scope), ChatMessage.role == "assistant")
            if message_id:
                query = query.where(ChatMessage.id == message_id)
            row = session.scalar(query
                .order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).limit(1))
            if row is None:
                return False
            identifier = row.id
            before = (row.body_md, row.actions_json, row.citations_json)
            metadata = json.loads(row.citations_json or "[]")
            nonce = next((item[_RID] for item in metadata if _RID in item), None)
            request = _request_message(session, scope, nonce)
            if request and nonce and not any(_REPLY_TO in item for item in metadata):
                row.citations_json = json.dumps([*metadata, {_REPLY_TO: request.id}])
            if not attach(venture_id, row, session=session, scope=scope, request=request.body_md if request else ""):
                return False
            request_id = request.id if request else None
            prepared = (row.body_md, row.actions_json, row.citations_json)
        with db()() as session:
            serialize_writes(session, key="chat-messages:" + scope.id)
            venture = session.get(Venture, venture_id)
            if venture is None or venture.archived_at:
                return False
            row = session.scalar(query.order_by(ChatMessage.created_at.desc(), ChatMessage.id.desc()).limit(1))
            if (row is None or (row.id, row.venture_id, row.workspace_id) !=
                    (identifier, venture_id, scope.workspace_id)
                    or (row.body_md, row.actions_json, row.citations_json) != before):
                return False
            current = _request_message(session, scope, nonce)
            if (current.id if current else None) != request_id:
                return False
            row.body_md, row.actions_json, row.citations_json = prepared
            session.commit()
        return True
    except Exception as exc:
        log_founder_exception(exc, venture_id, "next_moves.post")
        return False
