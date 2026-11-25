"""Never stuck: one path for every founder-facing failure, an alternative, and the founder's own result.

* ``decorate`` / ``record`` run inside ``post_milestone`` for every failure message (kind ending
  ``_failed`` or ``mac_task_stopped``), so a failure cannot reach the founder without an analytics
  receipt (``recovery.failure_shown`` + the ``error_event`` log line) and a "Do it another way" button.
  ``post_failure`` is the named entry point for new call sites.
* ``alternative`` (chat action ``recovery.alternative``): Charlie picks an automatic fallback Supymem can run
  itself (offered as a one-tap card), else a do-it-yourself guide with a "Submit my result" button.
* ``submit`` (chat action ``recovery.submit``): the founder's result is saved through the same tables the
  automated step writes, marked ``founder_provided``, and never marked verified.
"""
from __future__ import annotations

from app.ventures.current_idea import current_idea

import asyncio
import json
import logging
import re
import sys
from dataclasses import replace
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import event, select

log = logging.getLogger(__name__)


_SAFE_ERROR_CODES = frozenset({"insufficient_credits", "credits_unavailable", "account_suspended", "duplicate_ledger_entry",
    "bad_api_key", "rate_limited", "out_of_credits", "provider_down", "provider_error",
    "provider_outcome_unknown", "provider_output_invalid", "model_not_configured",
    "auth_required", "credit_required", "plan_expired", "update_required",
    "gtm_enrichment_deferred", "gtm_enrichment_slot_unavailable", "gtm_discovery_slot_unavailable",
    "gtm_operation_in_progress", "research_interrupted", "outreach_invitation_limits"})


def _safe_error_code(exc: BaseException) -> str | None:
    from app.core.jobs import RetryableError
    from app.credits.errors import CreditError
    from llm_gateway.errors import ProviderCallError

    code = (exc.args[0] if isinstance(exc, RetryableError) and len(exc.args) == 1 else
            getattr(exc, "code", None) if isinstance(exc, (CreditError, ProviderCallError, ValueError)) else None)
    return code if isinstance(code, str) and code in _SAFE_ERROR_CODES else None


def log_founder_exception(exc: BaseException, venture_id: str | None, action: str) -> None:
    from app.core.jobs import FatalError
    from pydantic import ValidationError

    # Validation errors can contain the founder's entire idea or provider credentials.
    extra = {"venture_id": venture_id, "action": action, "error_type": type(exc).__name__, "error_code": _safe_error_code(exc)}
    extra["reason_code"] = extra["error_code"] or "chat_action_failed"
    if isinstance(exc, TimeoutError):
        extra["reason_code"] = "chat_reply_timeout"
    elif isinstance(exc, asyncio.CancelledError):
        extra["reason_code"] = "chat_reply_cancelled"
    elif isinstance(exc, (RuntimeError, ValueError)) and exc.args in (("empty_reply",), ("chat_reply_empty",), ("chat_reply_missing",)):
        extra["reason_code"] = "chat_reply_empty"
    elif isinstance(exc, FatalError) and exc.args == ("recovery_lease_lost",):
        extra["reason_code"] = "chat_reply_lease_lost"
    frames = []
    traceback = exc.__traceback__
    while traceback is not None:
        frames.append(f"{traceback.tb_frame.f_globals.get('__name__', '<unknown>')}:{traceback.tb_lineno}")
        traceback = traceback.tb_next
    if not frames:
        caller = sys._getframe(1)
        frames.append(f"{caller.f_globals.get('__name__', '<unknown>')}:{caller.f_lineno}")
    extra.update(error_location=frames[-1],
                 error_frames=frames, diagnostic_code=extra["error_code"] or "chat_action_failed")
    safe_error = Exception(f"{extra['error_type']} at {extra['error_location']} code={extra['diagnostic_code']}")
    if cause := exc.__cause__ or exc.__context__:
        extra.update(cause_type=type(cause).__name__, cause_code=_safe_error_code(cause))
    if isinstance(exc, ValidationError):
        extra["validation_errors"] = [{"loc": error["loc"][:-1] + ("<extra>",) if error["type"] == "extra_forbidden" else error["loc"],
            "type": error["type"]}
            for error in exc.errors(include_input=False, include_context=False, include_url=False)]
    message = "founder_action_failed"
    if "validation_errors" in extra:
        message += " validation_errors=" + json.dumps(extra["validation_errors"])
    log.exception(message, extra=extra, exc_info=(type(safe_error), safe_error, None))
    from core.local_diagnostics import write_error
    write_error(exc, code=extra["reason_code"])


def founder_failure(exc: Exception, venture_id: str, what: str, retry_action: dict, *, safety: str = "") -> dict:
    from app.credits.errors import InsufficientCredits
    from app.llm.founder_provider import FounderLLMConfigError
    from app.llm.provider_errors import hosted_chat_timeout, local_provider_next_step
    from app.core.providers_local import LocalProviderError
    from llm_gateway.errors import ProviderCallError
    from llm_gateway.hosted import HostedCreditsRequired

    log_founder_exception(exc, venture_id, retry_action.get("action", "chat.reply"))
    from app.core.local_mode import is_local
    from app.credits.local_meter import LocalSpendLimit
    if is_local() and isinstance(exc, LocalSpendLimit):
        return {"kind": "text", "body_md": str(exc), "actions": [], "citations": []}
    if _safe_error_code(exc) == "outreach_invitation_limits":
        return {"kind": "text", "body_md": "Your invitation cannot fit all your limits. Ask me to shorten the wording of your invitation instructions while keeping your limits, then ask me to prepare your invitations again. Nothing was sent.",
                "actions": [], "citations": []}
    if isinstance(exc, (InsufficientCredits, HostedCreditsRequired)):
        from app.chat.blockers import explain
        return explain(venture_id, {"code": "insufficient_credits"})
    elif is_local() and isinstance(exc, (ProviderCallError, FounderLLMConfigError, LocalProviderError)):
        step = local_provider_next_step(exc.code if isinstance(exc, ProviderCallError) else "not_configured")
        body = f"Your AI connection couldn't finish {what}. {step}"
        if hosted_chat_timeout(exc):
            body += " Check your saved work before trying again. Nothing was retried."
            retry_action = {"action": "status", "payload": {}}
    elif hosted_chat_timeout(exc):
        body = "I couldn't confirm what finished. Choose Retry to check your saved work."
        retry_action = {"action": "status", "payload": {}}
    elif isinstance(exc, (ProviderCallError, FounderLLMConfigError)):
        body = f"Your AI connection couldn't finish {what}. Check your model settings, then retry."
    elif is_local() and isinstance(exc, OSError):
        body = ("I couldn't read or write your saved work. Check folder permissions in "
                "Settings > Keys and connections > Your data folder and backup.")
    else:
        body = f"I couldn't prepare {what}. Choose Retry to check your saved work."
    if safety:
        body += " " + safety
    return {"kind": "text", "body_md": body, "actions": [{**retry_action, "label": "Retry", "retry": True}], "citations": []}


def request_outcome_unknown() -> dict:
    from app.core.local_mode import is_local
    from app.llm.provider_errors import LOCAL_PROVIDER_SCREEN

    setup = f" If your AI provider is not responding, test your connection in {LOCAL_PROVIDER_SCREEN}." if is_local() else ""
    return {"error": {"code": "chat_request_outcome_unknown",
        "message": "You do not have a confirmed result yet. Retry this same message to check your saved reply without starting the work again." + setup,
        "actions": [{"label": "Retry", "action": "status", "payload": {}, "retry": True}]}}


def recover_request_reply(admission) -> dict | None:
    """Recover only the original failure and its settled receipts, never dispatch work."""
    from app.chat.store import idempotent_message
    from app.credits import metered
    from app.db.models import CreditReservation, EventLog
    from app.state import db

    try:
        saved = idempotent_message(admission.venture_id, "assistant", "chat-request-failure:" + admission.id)
        if saved is None or saved.get("kind") != "error":
            return None
        with db()() as session:
            events = session.scalars(select(EventLog).where(
                EventLog.workspace_id == admission.workspace_id, EventLog.venture_id == admission.venture_id,
                EventLog.topic == "credits.model.receipt.begin")).all()
            keys = []
            for event in events:
                request_id = json.loads(event.payload_json)["usage"].get("request_id")
                if isinstance(request_id, str) and (request_id == admission.id or request_id.startswith(admission.id + ":")):
                    keys.append(event.id.removesuffix(":begin"))
        if not keys:
            return None
        for key in keys:
            result = metered.recover_model_call(key)
            if not result or result.get("outcome") not in {"confirmed", "rejected", "platform_absorbed"}:
                return None
        with db()() as session:
            held = session.scalar(select(CreditReservation.id).where(
                CreditReservation.workspace_id == admission.workspace_id, CreditReservation.venture_id == admission.venture_id,
                (CreditReservation.request_id == admission.id) | CreditReservation.request_id.startswith(admission.id + ":", autoescape=True),
                CreditReservation.status.in_({"active", "open", "reconciling"})).limit(1))
            if held:
                return None
        return saved
    except Exception as exc:
        log_founder_exception(exc, admission.venture_id, "chat.request.recover")
        return None

ALTERNATIVE, SUBMIT = "recovery.alternative", "recovery.submit"
FAILURE_KINDS = frozenset({"mac_task_stopped"})
KIND_STEP = {"plan_failed": "plan.compile", "research_failed": "research.deep", "idea_failed": "idea.clarify_turn",
             "idea_validation_failed": "idea.validate", "build_failed": "build", "site_publish_failed": "publish",
             "mac_task_stopped": "mac.task", "content_failed": "content.draft", "action_failed": "chat.action",
             "gtm_failed": "gtm.diagnosis", "gtm_search_failed": "gtm.journey.prepare", "import_failed": "import.analyze", "experiment_failed": "experiment.evaluate",
             "task_failed": "task.run", "host_failed": "host.deploy", "provider_failed": "chat.provider"}
TEXT_CAP, URL_CAP, MAX_FINDINGS = 6000, 500, 25

# step -> analytics feature, automatic alternatives Charlie may choose, and the static do-it-yourself guide
# used when the model is unavailable (the model may rewrite it for this venture; it never loses it).
STEPS: dict[str, dict[str, Any]] = {
    "idea.board": {"feature": "idea", "what": "your independent idea review", "auto": (),
        "steps": ["Open Idea and copy your saved description, customer, and offer.",
                  "Ask three people to review it separately, without seeing each other's notes.",
                  "Ask each for the largest risk, the questions they need answered, and what would change their mind.",
                  "Compare their notes, keeping both shared risks and disagreements.",
                  "Paste the three separate reviews here with Submit my result. These are your review notes, not a completed AI review."],
        "bring": "Three separate reviews and the risks they share or disagree on."},
    "gtm.journey.prepare": {"feature": "gtm", "what": "your customer search", "auto": (),
        "steps": ["Search public sources for your audience and problem, or ask people you already know where they gather.",
                  "Check each source for fit and its published contact route or community participation rules.",
                  "Save the source links and draft a short research question. Do not send or post without checking permission."],
        "bring": "Public source links, why each fits your audience, and a draft research question."},
    "plan.compile": {"feature": "planning", "what": "your plan", "auto": ("plan_template",),
        "steps": ["Open any doc (Google Docs, Notion, Notes) and write a one-page plan.",
                  "Use this outline: the goal in one sentence; who your first customer is; 3 to 5 milestones, each with "
                  "one concrete action and how you will know it is done; the first thing you will do this week.",
                  "Paste the plan here with Submit my result."],
        "bring": "Your one-page plan as plain text."},
    "research.deep": {"feature": "research", "what": "your research", "auto": (),
        "steps": ["Search the web for: \"{idea} alternatives\", \"{idea} competitors\" and \"{idea} pricing\".",
                  "From each search pick the 5 most relevant results.",
                  "Write one line per result: the link, then one sentence on what it is and how it compares."],
        "bring": "Up to 15 lines, each with a link and one sentence."},
    "idea.validate": {"feature": "idea", "what": "your validation answers", "auto": (),
        "steps": ["Write one sentence each: who has this problem, how they solve it today, what would make them pay.",
                  "Name the single riskiest assumption, and one cheap way to test it this week.",
                  "Paste your answers here with Submit my result."],
        "bring": "Your answers, a few sentences in total."},
    "build": {"feature": "builds", "what": "your landing page", "auto": ("site_template",),
        "steps": ["Open Carrd, Google Sites or Notion and start a one-page site.",
                  "Add a headline, three benefits and one email sign-up or contact button.",
                  "Publish it and copy the web address."],
        "bring": "The landing page's web address (https link)."},
    "publish": {"feature": "publish", "what": "your published landing page", "auto": (),
        "steps": ["Pick a host you already use (Carrd, Netlify Drop, GitHub Pages, Google Sites).",
                  "Upload or publish your landing page there and open the public link to check it loads.",
                  "Copy the public web address."],
        "bring": "The public web address (https link)."},
    "content.draft": {"feature": "content", "what": "your content draft", "auto": (),
        "steps": ["Write 3 to 5 sentences: a hook, one useful point about your idea, and one question for readers.",
                  "Read it aloud once and cut anything you would not say to a customer.",
                  "Paste the draft here with Submit my result. Nothing is posted for you."],
        "bring": "The draft text."},
    "mac.task": {"feature": "computer_use", "what": "your Mac task result", "auto": (),
        "steps": ["Do the task by hand on your Mac.",
                  "Take a screenshot or note the key result.",
                  "Tell me what you did and what happened."],
        "bring": "A short note of what you did and the result."},
    "gtm.diagnosis": {"feature": "gtm_diagnosis", "what": "your go-to-market notes", "auto": (),
        "steps": ["List the three places your first customers already spend time (a forum, a group, a newsletter).",
                  "Write one sentence on what you would say to a person there.",
                  "Pick one place and send that message to 5 people yourself."],
        "bring": "The three places and who you will contact first."},
    "import.analyze": {"feature": "import", "what": "your project notes", "auto": (),
        "steps": ["Open the project folder and list its main parts (app, database, deploy).",
                  "Write what it does for users in two sentences and what is unfinished.",
                  "Paste that summary here with Submit my result."],
        "bring": "A short summary of what the project does and what is missing."},
    "experiment.evaluate": {"feature": "experiments", "what": "your experiment result", "auto": (),
        "steps": ["Write down the number you expected and the number you got.",
                  "Say in one sentence whether the idea held up and what surprised you.",
                  "Paste that here with Submit my result."],
        "bring": "Expected result, actual result and one sentence of what you learned."},
    "task.run": {"feature": "execution", "what": "your task result", "auto": (),
        "steps": ["Do the task by hand with a free tool you already use.",
                  "Note what you did and what you got.",
                  "Paste the note here with Submit my result."],
        "bring": "A short note of what you did and the result."},
    "host.deploy": {"feature": "hosting", "what": "your hosted product", "auto": (),
        "steps": ["Open https://vercel.com/new in your browser and click Continue with Google (or GitHub).",
                  "Click Import next to your project's repository (or drag your project folder onto the page). Leave the defaults.",
                  "Click Deploy and wait about a minute. Click the preview image to open your live site.",
                  "Copy the web address from your browser and paste it here with Submit my result."],
        "bring": "Your live product's web address (https link)."},
    "chat.provider": {"feature": "chat", "what": "your answer", "auto": (),
        "steps": ["Write down your question or request in one or two sentences.",
                  "Ask me again here in one short sentence, for example \"show my research\" or \"approve the plan\".",
                  "If you are stuck, paste what you tried here with Submit my result."],
        "bring": "What you did and where you got stuck."},
    "idea.clarify_turn": {"feature": "idea", "what": "your idea answers", "auto": (),
        "steps": ["Write one sentence each: who has this problem, what they do today, and what you would offer.",
                  "Add the one thing you are least sure about.",
                  "Paste that here with Submit my result."],
        "bring": "Your answers, a few sentences in total."},
    "chat.action": {"feature": "chat", "what": "your result", "auto": (),
        "steps": ["Do this step by hand with a free tool you already use.",
                  "Note what you did and the result.",
                  "Paste the note here with Submit my result."],
        "bring": "A short note of what you did and the result."},
}
STEPS["idea.board"] = {**STEPS["chat.action"], "what": "your idea review"}
STEPS["ceo_review"] = {**STEPS["idea.board"]}
_OTHER = {**STEPS["chat.action"]}


def review_failure_reply() -> dict:
    return {"kind": "navigation", "citations": [], "action_started": False,
            "body_md": "Your idea review did not finish because of a problem on our side. Your saved idea is kept. "
                       "Open Account settings and choose Report a problem. You can also interview one potential customer about how they handle the problem today and share their answers here.",
            "actions": [{"label": "Report a problem in Account settings", "href": "/account", "kind": "recovery_link"}]}


def _step(step: object) -> str:
    return step if isinstance(step, str) and step in STEPS else "chat.action"


def _spec(step: object) -> dict[str, Any]:
    return STEPS.get(step if isinstance(step, str) else "", _OTHER)


def button(step: str, kind: str = "", job_id: str = "", code: str = "", **extra: Any) -> dict[str, Any]:
    from app.chat.failed_jobs import clean_code

    payload = {"step": _step(step), "kind": str(kind or "")[:48], "job_id": str(job_id or "")[:64],
               "error_code": clean_code(code), **extra}
    return {"label": "Do it another way", "action": ALTERNATIVE, "payload": payload, "style": "secondary"}


def is_failure_kind(kind: str) -> bool:
    return kind.endswith("_failed") or kind in FAILURE_KINDS


def decorate(actions: list | None, kind: str, failure: dict | None) -> list:
    """Retry stays first; "Do it another way" is always present on a failure message."""
    failure = failure or {}
    actions = list(actions or [])
    if not any(a.get("action") == ALTERNATIVE for a in actions if isinstance(a, dict)):
        step = failure.get("step") or KIND_STEP.get(kind) or "chat.action"
        actions.append(button(step, kind, failure.get("job_id") or "", failure.get("code") or ""))
    return actions


def _workspace(venture_id: str) -> str | None:
    from app.db.models import Venture
    from app.state import db

    with db()() as session:
        venture = session.get(Venture, venture_id)
        return venture.workspace_id if venture else None


def record(venture_id: str, *, kind: str, key: str, failure: dict | None, workspace_id: str | None,
           session: Any = None) -> None:
    """Analytics receipt + ``error_event`` line for one founder-facing failure. Never raises."""
    try:
        from app.analytics.error_events import closed_code, closed_label, log_error_event
        from app.analytics.events import emit_in_session, emit_once
        from app.chat.failed_jobs import clean_code

        failure = failure or {}
        step = _step(failure.get("step") or KIND_STEP.get(kind))
        code = clean_code(failure.get("code")) or "unspecified"
        feature = STEPS[step]["feature"]
        props = {"step": step, "kind": closed_label(kind), "error_code": closed_code(code), "feature": feature}
        if failure.get("job_id"):
            props["job_id"] = closed_code(str(failure["job_id"]), "")
        event_key = re.sub(r"[^a-z0-9_:-]", "", f"{kind}:{key}".lower())[:160] or "unknown"
        if session is not None:
            emit_in_session(session, "recovery.failure_shown", event_key=event_key, workspace_id=workspace_id,
                            venture_id=venture_id, props=props)
        else:
            emit_once("recovery.failure_shown", event_key=event_key, workspace_id=workspace_id,
                      venture_id=venture_id, props=props)
        pending = {"feature": feature, "code": code, "venture_id": venture_id, "step": step, "kind": closed_label(kind)}

        def report(_session=None, transaction=None):
            if not pending or (transaction is not None and transaction.parent is not None):
                return
            alarm = dict(pending)
            pending.clear()
            try:
                log_error_event("recovery", **alarm)
            except Exception as exc:
                log_founder_exception(exc, venture_id, "recovery.record")

        if session is not None:
            event.listen(session, "after_transaction_end", report)
            event.listen(session, "after_rollback", lambda _session: pending.clear(), once=True)
        else:
            report()
    except Exception as exc:  # noqa: BLE001 - a receipt never blocks the message that explains the failure
        log_founder_exception(exc, venture_id, "recovery.record")


def post_failure(venture_id: str, *, step: str, key: str, body_md: str, kind: str, code: str = "",
                 job_id: str = "", actions: list | None = None, session: Any = None, exhausted: bool = False,
                 requested: bool = False) -> bool:
    """The one way to tell a founder something failed: plain text, Retry if given, Do it another way, a receipt.

    A human-only code (login, captcha, credit cap...) or ``exhausted`` (no route left) also asks the founder via
    needs_you; every other failure stays autonomous and never pings.
    """
    from app.chat.milestones import post_milestone

    posted = post_milestone(venture_id, key=key, body_md=body_md, actions=actions, kind=kind, session=session,
                            failure={"step": step, "code": code, "job_id": job_id}, requested=requested)
    if posted and session is None:
        from app.needs_you.hooks import from_failure

        from_failure(venture_id, step=step, code=code, ref=job_id or key, exhausted=exhausted)
    return posted


# ------------------------------------------------------------------ analytics for the rest of the loop

def _emit(name: str, venture_id: str, props: dict[str, Any], actor: str | None = None) -> None:
    try:
        from app.analytics.events import emit

        emit(name, person_id=actor if actor and actor not in {"system", "anonymous", "local", "founder"} else None,
             workspace_id=_workspace(venture_id), venture_id=venture_id, props=props)
    except Exception as exc:  # noqa: BLE001
        log_founder_exception(exc, venture_id, "recovery.emit")


# ------------------------------------------------------------------ alternative

def _reply(body: str, actions: list | None = None, kind: str = "text") -> dict[str, Any]:
    return {"kind": kind, "body_md": body, "actions": actions or [], "citations": []}


def _venture_line(venture_id: str) -> tuple[str, str]:
    from app.db.models import Venture
    from app.state import db

    with db()() as session:
        v = session.get(Venture, venture_id)
        return (v.name or "", current_idea(v, session=session) or "") if v else ("", "")


def _template_ready(venture_id: str, workspace_id: str | None) -> bool:
    from app.planning import execution as ex
    from app.state import db

    try:
        with db()() as session:
            ex.inputs(session, workspace_id, venture_id)
        return True
    except Exception as exc:  # noqa: BLE001 - not ready (no confirmed idea or research): offer the guide instead
        log_founder_exception(exc, venture_id, "recovery.template")
        return False


def _auto_card(venture_id: str, auto_id: str) -> dict[str, Any] | None:
    from app.chat.actions import get_action
    from app.chat.confirm import ConfirmationCard

    if auto_id == "plan_template":
        card = ConfirmationCard.for_action(
            get_action("recovery.plan_template"),
            interpretation="Build a starter plan from the standard first-customer journey, without an AI call.",
            consequences=["Saves a plan draft for you to review; nothing runs until you approve it.",
                          "Every step is yours to do and tick off. It is a template, not a plan tailored to your research."])
        return replace(card, confirm_label="Use the starter plan").as_action()
    if auto_id == "site_template":
        from app.chat.site_actions import rewrite_card

        return rewrite_card("Rebuild the landing page from the saved idea using the standard layout",
                            label="Rebuild the landing page")
    return None


async def _llm(messages: list[dict[str, str]], workspace_id: str | None) -> str:
    from app.chat.assistant import _complete

    return await _complete(messages, workspace_id=workspace_id)


def queue_alternative(venture_id: str, payload: dict[str, Any], workspace_id: str, actor: str | None) -> dict[str, Any]:
    from app.core.jobs import enqueue
    from app.chat.store import CLIENT_REQUEST_ID
    from app.state import db
    from core.ids import new_id

    if _step(payload.get("step")) in {"idea.board", "ceo_review"}:
        return review_failure_reply()
    with db()() as session:
        job_id = enqueue(session, "chat.recovery", {"recovery_payload": payload, "_actor_subject": actor},
                         workspace_id=workspace_id, venture_id=venture_id, max_attempts=1,
                         dedupe_key=f"chat.recovery:{venture_id}:{CLIENT_REQUEST_ID.get() or new_id('act')}")
        session.commit()
    return {**_reply("I am preparing another way from your saved work. I will post the result here; nothing is being retried or sent."),
            "kind": "recovery_pending", "idempotency_key": f"recovery-pending:{job_id}"}


async def run_alternative(payload: dict[str, Any]) -> None:
    from app.core.jobs import FatalError, _leased_row
    from app.chat.store import append_message
    from app.state import db

    with db()() as session:
        job = _leased_row(session, payload["_job_id"], payload["_worker_id"], payload["_job_attempt"])
        if not job or job.kind != "chat.recovery" or job.cancel_requested or (job.workspace_id, job.venture_id) != (payload["workspace_id"], payload["venture_id"]):
            raise FatalError("recovery_lease_lost")
        saved = json.loads(job.payload_json)
        if saved.get("_runtime"):
            raise FatalError("job_outcome_uncertain")
        saved["_runtime"] = {"operations": {"alternative": {"state": "calling"}}}
        job.payload_json = json.dumps(saved)
        session.commit()
    result = await alternative(payload["venture_id"], saved["recovery_payload"], payload["workspace_id"], saved.get("_actor_subject"))
    def output_authority(session, scope):
        job = _leased_row(session, payload["_job_id"], payload["_worker_id"], payload["_job_attempt"])
        if not job or job.cancel_requested or (job.workspace_id, job.venture_id) != (scope.workspace_id, scope.venture_id):
            raise FatalError("recovery_lease_lost")

    append_message(payload["venture_id"], role="assistant", body_md=result["body_md"], kind=result.get("kind", "text"),
                   actions=result.get("actions"), citations=result.get("citations"),
                   idempotency_key=f"recovery-result:{payload['_job_id']}", output_authority=output_authority)


def _parse(raw: str, offered: tuple[str, ...]) -> dict[str, Any] | None:
    """The model may only choose an offered alternative; steps are clipped. Anything else is rejected."""
    m = re.search(r"\{.*\}", raw or "", re.S)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    steps = [re.sub(r"\s+", " ", s).strip()[:260] for s in data.get("steps") or [] if isinstance(s, str) and s.strip()][:7]
    choice = data.get("choice") if data.get("choice") in offered else "diy"
    intro = re.sub(r"\s+", " ", str(data.get("intro") or "")).strip()[:400]
    bring = re.sub(r"\s+", " ", str(data.get("bring_back") or "")).strip()[:240]
    return {"choice": choice, "intro": intro, "steps": steps, "bring": bring}


async def alternative(venture_id: str, payload: dict[str, Any] | None, workspace_id: str | None = None,
                      actor: str | None = None) -> dict[str, Any]:
    """"Do it another way": automatic fallback card if one fits, else a guide the founder can follow today."""
    from app.chat.blockers import error_copy
    from app.chat.failed_jobs import clean_code

    payload = payload or {}
    step = _step(payload.get("step"))
    spec = STEPS[step]
    if step in {"idea.board", "ceo_review"}:
        return review_failure_reply()
    code = clean_code(payload.get("error_code"))
    workspace_id = workspace_id or _workspace(venture_id)
    from app.idea import validation_recovery
    if step == "idea.validate" and validation_recovery.rejected_request(venture_id, workspace_id, payload):
        try:
            guide = validation_recovery.guide(venture_id, workspace_id, payload.get("job_id"))
        except (ValueError, KeyError, TypeError):
            return _reply("That failed review no longer matches the current idea revision. Open Idea and review the current saved state; nothing was started.",
                          [{"label": "Open idea", "href": f"/v/{venture_id}/idea", "style": "primary"}])
        _emit("recovery.alternative_offered", venture_id, {"step": step, "mode": "diy", "choice": "diy",
              "llm": False, "feature": spec["feature"]}, actor)
        return _reply(guide["body"], [{"label": "Submit my result", "action": SUBMIT, "style": "primary",
            "payload": {"step": step, "job_id": payload["job_id"], "error_code": "research_invalid_output"}},
            {"label": "Open idea", "href": f"/v/{venture_id}/idea", "style": "secondary"}])
    name, idea = _venture_line(venture_id)
    offered = tuple(a for a in spec["auto"] if payload.get("mode") != "diy"
                    and (a != "plan_template" or _template_ready(venture_id, workspace_id)))
    plan: dict[str, Any] | None = None
    try:
        system = ("You are Charlie, the founder's AI manager. A step failed on our side. Pick the best way to keep the "
                  "venture moving. Reply with JSON only: {\"choice\": one of " + json.dumps(list(offered) + ["diy"]) +
                  ", \"intro\": one plain sentence, \"steps\": up to 6 short numbered-step texts using free common tools, "
                  "\"bring_back\": exactly what to paste back}. Prefer an offered automatic option when one fits; otherwise "
                  "\"diy\". Never claim anything is running or done, never tell the founder to wait, never use jargon.")
        context = (f"Venture: {name or 'unnamed'}. Idea: {idea[:600]}\nFailed step: {step}\n"
                   f"Reason: {error_copy(code) or 'not recorded'}\nTemplate guide to adapt: {json.dumps(spec['steps'])}")
        plan = _parse(await _llm([{"role": "system", "content": system}, {"role": "user", "content": context}],
                                 workspace_id), offered)
    except Exception as exc:  # noqa: BLE001 - the founder still gets the static guide
        log_founder_exception(exc, venture_id, "recovery.alternative")
    choice = (plan or {}).get("choice") or (offered[0] if offered else "diy")
    mode = "auto" if choice != "diy" else "diy"
    _emit("recovery.alternative_offered", venture_id, {"step": step, "mode": mode, "choice": choice,
          "llm": bool(plan), "feature": spec["feature"]}, actor)
    if mode == "auto" and (card := _auto_card(venture_id, choice)):
        body = (plan or {}).get("intro") or "I can do this another way myself. Confirm and I will start; nothing is published."
        diy = {"label": "I will do it myself", "action": ALTERNATIVE, "style": "ghost",
               "payload": {**{k: v for k, v in payload.items() if k != "mode"}, "step": step, "mode": "diy"}}
        return _reply(body, [card, diy], "confirmation")
    steps = (plan or {}).get("steps") or [s.replace("{idea}", (idea or name or "your idea")[:80]) for s in spec["steps"]]
    bring = (plan or {}).get("bring") or spec["bring"]
    intro = (plan or {}).get("intro") or "Here is a way to get past this without waiting on us."
    lines = "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))
    submit = {"label": "Submit my result", "action": SUBMIT, "style": "primary",
              "payload": {"step": step, "kind": str(payload.get("kind") or "")[:48],
                          "job_id": str(payload.get("job_id") or "")[:64], "error_code": code}}
    return _reply(f"{intro}\n\n{lines}\n\n**Bring back:** {bring}\n\nI will save it as your own work, marked as "
                  "founder-provided and not verified, and we carry on from there.", [submit])


# ------------------------------------------------------------------ founder-provided result

def clean_text(value: object, cap: int = TEXT_CAP) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f]", "", str(value or "")).strip()[:cap]


def clean_url(value: object) -> str | None:
    """http(s) only, a real host, no embedded credentials, capped. Anything else is dropped."""
    text = clean_text(value, URL_CAP + 1)
    try:
        parts = urlsplit(text)
        ok = parts.scheme in {"http", "https"} and bool(parts.hostname) and "@" not in parts.netloc
    except ValueError:
        return None
    return text if ok and len(text) <= URL_CAP and not re.search(r"\s", text) else None


_URL = re.compile(r"https?://\S+")


def _skeleton(summary: str, note: str) -> dict[str, Any]:
    """A valid nine-step proposal made only of founder actions, so it needs no AI call and claims no automation."""
    from app.planning.author import GENERATED_JOURNEY

    steps = {"landing": ("Put up a one-page site", "Make a simple landing page that says who it is for and what it offers."),
             "validation": ("Check the problem with real people", "Talk to a few people who have the problem."),
             "core": ("Build the smallest useful version", "Make or fake the smallest thing that tests the idea."),
             "monetisation": ("Test what people will pay", "Ask for a price in a real conversation."),
             "acquisition": ("Reach the first customers", "Pick one channel and message people directly."),
             "first_users": ("Get the first users in", "Onboard a handful of people by hand."),
             "feedback": ("Collect feedback", "Ask each user what almost stopped them."),
             "iteration": ("Change one thing", "Fix the biggest problem you heard."),
             "first_paid_user": ("Win the first paid user", "Ask the best-fit user to pay.")}
    milestones = []
    for i, kind in enumerate(GENERATED_JOURNEY, 1):
        title, text = steps[kind]
        milestones.append({"key": f"m{i}", "kind": kind, "title": title, "description": text, "included": True,
            "depends_on": [f"m{i - 1}"] if i > 1 else [], "acceptance": [f"You can say what happened: {title.lower()}."],
            "tasks": [{"key": "t1", "title": title, "description": text, "capability_id": "founder.action",
                "implementation": "founder_action", "source_ids": [], "resource_ids": [],
                "acceptance": [f"Write down what you did and what you saw: {title.lower()}."],
                "founder_action": {"title": title, "steps": [text, "Write down what you did and what you saw."],
                    "verification": "Your own written note."}}]})
    return {"summary": summary[:1900], "goal_first_paid_user": "One real customer pays for the first version.",
        "target_days": None, "assumptions": [note[:900]] if note else [], "exclusions": [],
        "evidence_gaps": ["This plan was not tailored by AI and has not been checked against your research."],
        "milestones": milestones}


def create_plan(venture_id: str, workspace_id: str, *, provenance: str, founder_text: str = "") -> str:
    """Persist a template or founder-written plan through the compiler's own persistence, ready for approval."""
    from app.core.fsm import advance
    from app.db.models import Venture
    from app.db.supymem_models import ExecutionPlan
    from app.planning import author, execution as ex
    from app.planning.compiler import _persist
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id
    from sqlalchemy import func

    template = provenance == "template"
    summary = ("Starter plan from the standard first-customer journey. Every step is yours to do." if template
               else "Your own plan, saved as written. " + founder_text)
    note = "" if template else "Founder-provided plan text (not AI-authored): " + founder_text
    with db()() as session:
        session.get(Venture, venture_id, with_for_update=True)
        data = ex.inputs(session, workspace_id, venture_id)
        proposal = _skeleton(summary, note)
        from app.ventures.journey_overrides import read as journey_choices
        skipped = journey_choices(venture_id, session=session)["skipped"]
        if skipped:
            proposal["assumptions"].extend(data["context"]["research_gaps"])
        proposal = author.validate(proposal, data["context"])
        pid = new_id("pln")
        version = (session.scalar(select(func.max(ExecutionPlan.version)).where(
            ExecutionPlan.venture_id == venture_id)) or 0) + 1
        row = ExecutionPlan(id=pid, workspace_id=workspace_id, venture_id=venture_id, revision_id=data["revision_id"],
            version=version, state="draft", summary="", target_days=14, goal_first_paid_user="", created_at=utcnow(),
            structured="{}")
        session.add(row)
        session.flush()
        doc = {"method": "founder_provided_plan" if not template else "template_plan", "provenance": provenance,
               "edit_version": 0, "_runtime": {**data, "job_id": None, "operations": {}, "calls_used": 0}}
        _persist(session, row, doc, proposal)
        advance(session, row, "founder_review", reason="recovery_" + provenance)
        row.structured = json.dumps(doc, ensure_ascii=False)
        session.commit()
    from app.autopilot import receipts
    receipts.persisted_record(ExecutionPlan, pid, {"state": "founder_review"})
    return pid


def _idea(session, venture_id: str):
    from app.db.supymem_models import IdeaSession

    return session.scalars(select(IdeaSession).where(IdeaSession.venture_id == venture_id,
        IdeaSession.state != "abandoned").order_by(IdeaSession.updated_at.desc()).limit(1)).first()


def _save_research(venture_id: str, workspace_id: str, text: str) -> int:
    from app.db.supymem_models import ResearchDossier, ResearchFinding
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id

    rows = []
    for line in [ln.strip() for ln in text.splitlines() if ln.strip()][:MAX_FINDINGS]:
        found = _URL.search(line)
        url = clean_url(found.group(0).rstrip(".,;)")) if found else None
        rest = (line.replace(found.group(0), " ") if found else line).strip(" -:–—*•")
        rows.append((url, (urlsplit(url).hostname if url else rest[:80]) or "Founder note", rest[:600] or line[:600]))
    now = utcnow()
    with db()() as session:
        idea = _idea(session, venture_id)
        did = new_id("rsd")
        session.add(ResearchDossier(id=did, workspace_id=workspace_id, venture_id=venture_id,
            revision_id=(idea.confirmed_revision_id or idea.current_revision_id) if idea else None, kind="deep",
            state="partial", budget_calls=0, calls_used=0, created_at=now, completed_at=now,
            summary=json.dumps({"review_kind": "retrieved_sources_only", "provenance": "founder_provided",
                "coverage_gaps": ["Sources were found and summarised by the founder; Supymem did not fetch or verify them."]})))
        session.flush()
        for url, title, summary in rows:
            session.add(ResearchFinding(id=new_id("rsf"), dossier_id=did, workspace_id=workspace_id,
                venture_id=venture_id, category="other", title=title, url=url, source="founder_provided",
                summary=summary, created_at=now, evidence=json.dumps({"evidence_kind": "founder_provided",
                    "page_verified": False, "observed_at": now.isoformat(), "founder_provided": True})))
        session.commit()
    return len(rows)


def _save_validation(venture_id: str, workspace_id: str, text: str) -> bool:
    from app.db.supymem_models import ValidationRun
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id

    with db()() as session:
        idea = _idea(session, venture_id)
        if idea is None or not idea.current_revision_id:
            return False
        from app.db.supymem_models import IdeaRevision
        from app.idea.discovery_privacy import unsafe_discovery_request
        revision = session.get(IdeaRevision, idea.current_revision_id)
        if revision is None or unsafe_discovery_request({"note": text}, revision.structured):
            raise ValueError("validation_recovery_private_details")
        report = {"provenance": "founder_provided", "founder_answers": text, "strengths": [], "proposed_tests": [],
                  "gaps": ["Founder-provided answers: no AI or market check was run on them."]}
        session.add(ValidationRun(id=new_id("vlr"), session_id=idea.id, revision_id=idea.current_revision_id,
            workspace_id=workspace_id, venture_id=venture_id, method="founder_provided", state="complete",
            verdict="needs_evidence", report=json.dumps(report), created_at=utcnow(), completed_at=utcnow()))
        session.commit()
    return True


def _owns_task(venture_id: str, task_id: str) -> bool:
    from app.db.models import PlannedTask
    from app.state import db

    with db()() as session:
        row = session.get(PlannedTask, task_id) if task_id else None
        return bool(row and row.venture_id == venture_id)


def _save_document(venture_id: str, kind: str, title: str, text: str, step: str, *, url: str | None = None,
                   unique: bool = True, doc_id: str | None = None) -> None:
    from app.db.models import VentureDocument
    from app.state import db
    from core.clock import utcnow
    from core.ids import new_id

    now = utcnow()
    doc_id = doc_id or (new_id("doc") if unique else f"{kind}:{venture_id}")
    prov = json.dumps({"source": "founder_provided", "step": step, "verified": False})
    with db()() as session:
        row = session.get(VentureDocument, doc_id)
        if row is None:
            session.add(VentureDocument(id=doc_id, venture_id=venture_id, kind=kind, title=title, path=url,
                body_md=text, provenance_json=prov, created_at=now, updated_at=now))
        elif row.venture_id == venture_id:
            row.title, row.path, row.body_md, row.provenance_json, row.updated_at = title, url, text, prov, now
        session.commit()


EXTERNAL_NOTE = "Your own site (not checked by Supymem)"


def external_site(venture_id: str) -> dict[str, Any] | None:
    """The product site hosted outside Supymem, if any. Founder-built sites are never treated as verified;
    a hosted product is verified only when Core's own fetch passed (chat/hosting.py)."""
    from app.chat.service_guides import snapshot
    from app.db.models import VentureDocument
    from app.state import db

    with db()() as session:
        hosted = snapshot(session, venture_id)
        if hosted["verified"]:
            return {"url": hosted["product_url"], "verified": True, "host": hosted["host"],
                    "note": f"Your hosted product (verified by Supymem on {str(hosted['verified_at'])[:10]}: HTTP 200, title \"{hosted['title']}\")"}
        row = session.get(VentureDocument, f"external_site:{venture_id}")
        return {"url": row.path, "note": EXTERNAL_NOTE} if row and row.path and row.venture_id == venture_id else None


def founder_drafts(session, venture_id: str, limit: int = 5) -> list[dict[str, Any]]:
    """Content drafts the founder wrote themselves, shaped like the content drafts list."""
    from app.db.models import VentureDocument

    rows = session.scalars(select(VentureDocument).where(VentureDocument.venture_id == venture_id,
        VentureDocument.kind == "content_draft").order_by(VentureDocument.updated_at.desc()).limit(limit)).all()
    return [{"id": r.id, "platform": "founder", "state": "founder_provided", "title": (r.title or "")[:80] or None}
            for r in rows]


def submit(venture_id: str, payload: dict[str, Any] | None, workspace_id: str | None = None,
           actor: str | None = None) -> dict[str, Any]:
    """Save what the founder did themselves where the automated step would have saved its result, then continue."""
    from app.chat.handlers.general import _next_step_actions, _next_step_line
    from app.idea import validation_recovery

    payload = payload or {}
    step = _step(payload.get("step"))
    workspace_id = workspace_id or _workspace(venture_id)
    text, url = clean_text(payload.get("text")), clean_url(payload.get("url"))
    again = {"label": "Submit my result", "action": SUBMIT, "style": "primary", "payload": {
        k: payload.get(k, "") for k in ("step", "kind", "job_id", "error_code")}}
    if not text and not url:
        return _reply("I did not get anything to save. Paste your result (or a link) and send it again.", [again])
    if step in {"build", "publish", "host.deploy"} and not url:
        return _reply("I need a web address that starts with http:// or https:// for this one. Nothing was saved.", [again])
    if payload.get("url") and not url:
        return _reply("That link is not a valid http:// or https:// address, so I did not save anything.", [again])
    saved = "your result"
    try:
        if step == "plan.compile":
            if not text:
                return _reply("Paste the plan text so I can save it for your review. Nothing was saved.", [again])
            create_plan(venture_id, workspace_id or "", provenance="founder_provided", founder_text=text[:1500])
            saved = "your plan as a draft for you to review and approve"
        elif step == "research.deep":
            n = _save_research(venture_id, workspace_id or "", text or (url or ""))
            saved = f"your {n} research note{'s' if n != 1 else ''} as the research for this venture"
        elif step == "idea.validate" and validation_recovery.rejected_request(venture_id, workspace_id, payload):
            try:
                validation_recovery.save(venture_id, workspace_id, payload.get("job_id"), text)
            except (ValueError, KeyError, TypeError):
                return _reply("Nothing was saved. Use the current failed review and a short anonymized note (up to 1500 characters), without private records or contact details.", [again])
            _emit("recovery.result_submitted", venture_id, {"step": step, "feature": "idea", "has_url": False}, actor)
            return _reply("Saved your discovery note on the current idea revision as founder-provided and unverified. "
                          "The rejected review and its charged attempts remain unchanged. No idea was accepted or confirmed, "
                          "no research started, and no users or payment were verified. A later approved validation retry will read this note as attributed input.",
                          [{"label": "Open idea", "href": f"/v/{venture_id}/idea", "style": "primary"}])
        elif step == "idea.validate":
            if not _save_validation(venture_id, workspace_id or "", text):
                return _reply("I could not find an idea to attach your answers to, so nothing was saved. Open the idea page first.")
            saved = "your validation answers"
        elif step == "host.deploy":
            from app.chat import hosting

            _emit("recovery.result_submitted", venture_id, {"step": step, "feature": "hosting", "has_url": True}, actor)
            return hosting.verify(venture_id, url)
        elif step in {"build", "publish"}:
            _save_document(venture_id, "external_site", "Site built outside Supymem", text, step, url=url, unique=False)
            saved = "your site link (recorded as an outside site; Supymem has not checked it)"
        elif step == "content.draft":
            _save_document(venture_id, "content_draft", "Founder-written draft", text, step)
            saved = "your draft (not posted anywhere)"
        else:
            body = "\n".join(filter(None, [text, url]))
            task_id = str(payload.get("job_id") or "")
            if step == "task.run" and _owns_task(venture_id, task_id):  # shows where the task's own note would
                _save_document(venture_id, "task_note", f"Founder result: {task_id}", body, step,
                               doc_id=f"task-note:{task_id}")
            else:
                _save_document(venture_id, "founder_result", f"Founder result: {step}", body, step)
    except ValueError:
        if step == "idea.validate":
            return _reply("I could not save that validation note. Use an anonymized description without requesting private records, child observation or client contact lists.", [again])
        return _reply("I could not save that yet because the earlier steps it depends on are not saved. "
                      "Your text is still in your doc; open the venture to see what is missing.")
    _emit("recovery.result_submitted", venture_id, {"step": step, "feature": STEPS[step]["feature"],
          "has_url": bool(url)}, actor)
    line = _next_step_line(venture_id, workspace_id)
    return _reply(f"Saved {saved}. It is marked as founder-provided and not verified. " + (f"Next: {line}" if line else ""),
                  _next_step_actions(venture_id))
