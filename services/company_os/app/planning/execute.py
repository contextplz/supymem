"""Run a planned task and leave an artifact + chat note."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import select
from llm_gateway.errors import ProviderCallError

from app.actuation.contract import ActuationMode, ActuationResult
from app.chat.blockers import with_explain
from app.chat.store import append_message
from app.config import data_root
from app.db.models import PlannedTask, TaskFingerprint
from app.org.characters import decode_assignee, stamp_speaker
from app.org.manager import manager_speaker
from app.state import db
from core.clock import utcnow
from core.ids import new_id

_SOCIAL = (
    "social",
    "post",
    "twitter",
    "instagram",
    "linkedin",
    "facebook",
    "tiktok",
    "handle",
    "outreach",
)
_PAY = ("stripe", "razorpay", "paid offer", "payment account")
_GITHUB = ("github",)
_SITE = (
    "landing",
    "waitlist",
    "first look",
    "first-look",
    "firstlook",
    "value proposition",
    "website",
    "landing page",
)
_PROTOTYPE = (
    "prototype",
    "core workflow",
    "instrument",
    "activation funnel",
    "build",
    "code",
    "ship",
    "repo",
)
_RECRUIT = ("recruit", "design partner")
_RESEARCH = (
    "research",
    "competitor",
    "demand probe",
    "interview",
    "sourced",
    "competitor claim",
    "alternatives",
    "objection",
    "hypothesis",
    "customer journey",
    "pricing",
    "conversion evidence",
    "concierge test",
    "validate ",
    "compare ",
    "audit ",
)
PRODUCT_KINDS = frozenset({"site", "outreach", "prototype", "recruit"})


def _local_brief_only(title: str) -> bool:
    from app.core.local_mode import is_local

    # shortcut: brief-only intent must lead the title, upgrade when tasks carry typed work intent.
    return is_local() and bool(re.match(r"^(?:write|draft|prepare|summarize|update)\s+(?:(?:a|an|the)\s+)?"
        r"(?:research\s+)?(?:notes?|briefs?|requirements|evidence)\b", title.strip(), re.I))


def work_kind(title: str, tags: list | None = None, detail: str = "") -> str:
    """Classify planned work so a title-search cannot close product tasks."""
    from app.chat.steer import is_non_delivery_outreach

    if _local_brief_only(title) or is_non_delivery_outreach(title, detail):
        return "research"
    blob = _blob(title, tags or [])
    if detail:
        blob = f"{blob} {detail.lower()}"
    if _has(blob, _SOCIAL):
        return "outreach"
    if _has(blob, _RECRUIT):
        return "recruit"
    if _has(blob, _SITE) or " site" in f" {blob}" or blob.startswith("site "):
        return "site"
    if _has(blob, _PROTOTYPE):
        return "prototype"
    if _has(blob, _RESEARCH):
        return "research"
    return "other"


def is_product_work(title: str, tags: list | None = None, detail: str = "") -> bool:
    return work_kind(title, tags, detail) in PRODUCT_KINDS


class TaskRunError(Exception):
    def __init__(self, status: int, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def _venture_repo(venture_id: str) -> Path:
    path = data_root() / "ventures" / venture_id / "repo"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _blob(title: str, tags: list) -> str:
    return f"{title} {' '.join(str(t) for t in tags)}".lower()


def _has(blob: str, needles: tuple[str, ...]) -> bool:
    """Substring for phrases; word-boundary for short tokens so 'build' ≠ 'buildings'."""
    for needle in needles:
        if " " in needle or len(needle) >= 6:
            if needle in blob:
                return True
        elif re.search(rf"\b{re.escape(needle)}\b", blob):
            return True
    return False


def _is_private_email(title: str, detail: str = "") -> bool:
    from app.chat.steer import is_private_email_draft

    from app.tasks.contracts import requires_reviewed_action
    return not requires_reviewed_action(title, detail) and is_private_email_draft(title, detail)


def _is_interview_invitation_draft(title: str, detail: str = "") -> bool:
    """An unsent interview invitation is writing work, not web research."""
    blob = f"{title} {detail}".lower()
    return (bool(re.search(r"\bdraft\b", blob))
            and bool(re.search(r"\b(?:invitation|invite)\b", blob))
            and bool(re.search(r"\binterview\b", blob))
            and bool(re.search(r"\b(?:do not|don't)\s+(?:send|publish|contact)\b", blob)))


def _interview_invitation(venture_id: str, detail: str) -> str:
    """Produce a bounded review draft without inventing recipients or beta access."""
    from app.autopilot.outreach import founder_signature, idea_sentence
    from app.db.models import Venture

    with db()() as session:
        venture = session.get(Venture, venture_id)
        name = idea_sentence(venture.name if venture else "", 80) or "this venture"
    minutes = re.search(r"\b(10|15|20|30)[- ]minute\b", detail.lower())
    duration = minutes.group(1) if minutes else "15"
    beta_note = (
        "The product beta is not live yet; this is a research conversation, not an invitation to use it.\n\n"
        if re.search(r"\b(?:beta|product)\b.{0,35}\bnot (?:yet )?live\b", detail, re.IGNORECASE)
        else "This is a research conversation, not a promise of product access.\n\n"
    )
    return (
        "Status: draft for your review; not sent or published.\n\n"
        "## Interview invitation draft\n\n"
        f"Subject: Optional {duration}-minute conversation about {name}\n\n"
        "Hi,\n\n"
        f"I’m working on {name} and would value a {duration}-minute conversation "
        "about how you handle this problem today and what has or hasn’t worked for you. "
        "Would you be open to a short call? Participation is entirely optional.\n\n"
        + beta_note
        + "If you’re interested, please reply with a time that works for you.\n\n"
        f"Thank you,\n{founder_signature(venture_id)}\n\n"
        "## Before any outreach\n\n"
        "You must review the text, choose the exact consented recipients and sending "
        "connection, and separately approve sending. No recipient or reply is assumed."
    )


def _is_site_copy(title: str, detail: str = "", tags: list | None = None) -> bool:
    if work_kind(title, tags, detail) == "site":
        return True
    from app.chat.steer import is_site_copy_work

    return is_site_copy_work(title, detail, tags)


def _founder_gate(title: str, tags: list, venture_id: str | None = None, detail: str = "") -> dict | None:
    from app.chat.steer import is_non_delivery_outreach, is_review_draft

    from app.tasks.contracts import requires_reviewed_action
    public_research = (work_kind(title, tags, detail) == "research"
                       and not requires_reviewed_action(title, detail)
                       and not re.search(r"\b(?:private|confidential|internal)\s+(?:github|repository|repo|database|data)\b", f"{title} {detail}", re.I))
    if public_research:
        return None
    if not requires_reviewed_action(title, detail) and (is_non_delivery_outreach(title, detail) or is_review_draft(title, detail)):
        return None  # writing a draft for review needs no connection; sending or posting does
    blob = _blob(title, tags)
    if _has(blob, _SOCIAL):
        if venture_id:
            from app.actuation.social import founder_social_connected

            if founder_social_connected(venture_id):
                return None
        return {
            "provider": "social",
            "title": "Connect your social accounts",
            "why": "Live posts come from your own accounts. You sign in on each site; I never see your password.",
        }
    payment_scope = f"{blob} {detail.lower()}"
    base_product_work = bool(re.search(r"\b(?:website|landing|pricing|page|product|prototype|core workflow)\b", payment_scope))
    payment_excluded = bool(re.search(
        r"\b(?:stripe|razorpay|payments?|payment processing|payment collection|checkout)(?:\s+payments?)?\s+"
        r"(?:(?:is|are|remain|remains|stay|stays)\s+)?(?:disabled|excluded|deferred|out of scope|off|optional)\b"
        r"|\b(?:without|no)\s+(?:(?:stripe|razorpay)\s+)?(?:payments?|payment processing|payment collection|checkout)\b"
        r"|\b(?:exclude|defer|disable)\s+(?:(?:stripe|razorpay)\s+)?(?:payments?|payment processing|payment collection|checkout)\b",
        payment_scope))
    explicit_payment_task = bool(re.search(
        r"\b(?:connect|set up|enable|activate|integrate)\s+(?:the\s+)?(?:stripe|razorpay|payments?|checkout|payment account)\b"
        r"|\b(?:collect|charge|accept|process)\s+(?:real\s+|customer\s+)?payments?\b", title, re.I))
    if (any(needle in blob for needle in _PAY) or ("paid" in blob and "offer" in blob)) and not (
            base_product_work and payment_excluded and not explicit_payment_task):
        return {
            "provider": "razorpay" if "razorpay" in payment_scope else "stripe" if "stripe" in payment_scope else "payments",
            "title": "Connect a payment account",
            "why": "Customer payment collection stays disabled until its separately approved merchant setup is ready. "
                   "Ordinary website and product work do not require a payment account.",
        }
    if any(needle in blob for needle in _GITHUB):
        return {
            "provider": "github",
            "title": "Connect GitHub for a real repository",
            "why": "The repo stays local until a GitHub account is connected.",
        }
    from app.munder.adjudicate import classify_work

    verdict = classify_work(title, detail, tags)
    if verdict.kind == "critical":
        return {
            "provider": verdict.provider,
            "title": "This needs your call before we proceed",
            "why": (
                f"The hive treats this as {verdict.reason.replace('_', ' ')}"
                ", not something to do unsupervised."
            ),
        }
    return None


def _mark(speaker: dict | None, venture_id: str, *, working: str | None) -> None:
    if not speaker or not speaker.get("id"):
        return
    try:
        from app.org.heartbeat import mark_idle, mark_working

        if working:
            mark_working(str(speaker["id"]), venture_id, working)
        else:
            mark_idle(str(speaker["id"]), venture_id)
    except Exception:  # noqa: BLE001 — heartbeat must not fail the task
        pass


def _chat(venture_id: str, task_id: str, body: str, speaker: dict | None, *,
          kind: str = "task_run", actions: list | None = None, once: bool = False) -> None:
    if once:  # completion is a milestone: one post per task, retries never duplicate
        from app.chat.milestones import post_milestone
        post_milestone(venture_id, key=f"{kind}:{task_id}", body_md=stamp_speaker(body, speaker),
                       actions=actions, kind=kind)
        return
    append_message(
        venture_id,
        actions=actions,
        role="assistant",
        kind=kind,
        body_md=stamp_speaker(body, manager_speaker(venture_id)),
        idempotency_key=f"{kind}:{task_id}:{utcnow().isoformat()}",
    )


def _write_artifact(
    path: Path, title: str, body: str, speaker: dict | None, *,
    venture_id: str, task_id: str, email_revision_detail: str | None = None,
    research_receipt: dict | None = None,
) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    owner = speaker.get("name") if speaker else "Office"
    text = f"# {title}\n\nOwner: {owner}\n\n{body.strip()}\n"
    path.write_text(text, encoding="utf-8")
    # The API and worker run in different Fargate tasks with separate /tmp
    # filesystems. A local path in a chat completion is not a reviewable
    # artifact; keep the exact note in the venture-scoped durable database.
    from app.db.models import VentureDocument
    from app.db.session import serialize_writes

    now = utcnow()
    with db()() as session:
        serialize_writes(session, key=f"task-note:{task_id}")
        provenance = {"task_id": task_id, "source": "task.run"}
        if research_receipt:
            from hashlib import sha256
            row = session.get(PlannedTask, task_id)
            binding = _research_binding(session, row, research_receipt["run_id"], research_receipt["step_id"])
            if binding != {key: research_receipt[key] for key in binding}:
                raise TaskRunError(409, "research_inputs_changed", "The research request changed before its note was saved.")
            receipt = {**research_receipt, "body_sha256": sha256(text.encode()).hexdigest()}
            receipt["receipt_sha256"] = _research_hash(receipt)
            provenance["research_receipt"] = receipt
        if email_revision_detail:
            from app.chat.email_revision import assert_at_write
            try:
                assert_at_write(session, venture_id, email_revision_detail)
            except (ValueError, KeyError, TypeError):
                raise TaskRunError(409, "saved_interview_draft_changed", "Saved draft changed before the revised note could be stored.") from None
        row = session.get(VentureDocument, f"task-note:{task_id}")
        if row is None:
            session.add(VentureDocument(
                id=f"task-note:{task_id}", venture_id=venture_id,
                kind="task_note", title=title, path=None, body_md=text,
                provenance_json=json.dumps(provenance),
                created_at=now, updated_at=now,
            ))
        elif row.venture_id == venture_id and row.kind == "task_note":
            row.title, row.body_md, row.updated_at = title, text, now
            row.provenance_json = json.dumps(provenance)
        else:
            raise TaskRunError(409, "task_note_conflict", "Saved work has a conflicting owner.")
        session.commit()
    return text


def _close_done(session, row: PlannedTask) -> None:
    row.status = "done"
    row.finished_at = utcnow()
    row.updated_at = utcnow()
    row.last_error = None
    if row.fingerprint:
        fp = session.scalars(
            select(TaskFingerprint).where(
                TaskFingerprint.venture_id == row.venture_id,
                TaskFingerprint.fingerprint == row.fingerprint,
            )
        ).first()
        if fp:
            fp.status = "done"


def _park_incomplete(session, row: PlannedTask, reason: str) -> None:
    """Leave the task open. A search note is not completion."""
    row.status = "open"
    row.runnable = 0
    row.last_error = reason
    row.finished_at = None
    row.updated_at = utcnow()


def _record_ops_post(
    venture_id: str,
    *,
    content: str,
    result,
) -> None:
    from app.actuation.social import workspace_id_for
    from app.ops.services import social as social_svc

    workspace_id = workspace_id_for(venture_id)
    if not workspace_id:
        return
    from app.actuation.contract import ActuationMode

    live = getattr(result, "mode", None) is ActuationMode.LIVE
    social_svc.create_post(
        workspace_id,
        content=content,
        status="published" if live else "draft",
        platform="twitter",
        tweet_id=result.external_ref if live else None,
    )


def _social_draft_matches(result: ActuationResult, venture_id: str, title: str, detail: str) -> bool:
    from app.actuation.social import compose_post_text

    expected = {"content": compose_post_text(title=title, detail=detail), "publishNow": False, "title": title}
    return (isinstance(result, ActuationResult) and result.mode is ActuationMode.SIMULATED
            and result.ok is True and result.dry_run is True
            and result.tool_id == "social.post" and result.venture_id == venture_id
            and isinstance(result.provider_response, dict)
            and result.provider_response.get("payload") == expected)


async def _run_social_post(venture_id: str, title: str, detail: str) -> tuple[ActuationResult, str]:
    from app.actuation.execute import execute
    from app.actuation.social import compose_post_text

    content = compose_post_text(title=title, detail=detail)
    # Autopilot never publishes. Chat asks the founder first.
    result = await execute(
        "social.post", venture_id,
        {"content": content, "publishNow": False, "title": title}, allow_live=False,
    )
    if _social_draft_matches(result, venture_id, title, detail):
        try:
            _record_ops_post(venture_id, content=content, result=result)
        except Exception:  # noqa: BLE001 — task-note receipt is the completion boundary
            pass
        return result, "\n\n**Simulated** draft only. Nothing was published. Review the saved draft before separately approving delivery."
    if result.mode is ActuationMode.FAILED and result.reason == "no_connected_accounts":
        return result, "\n\nNo connected social accounts were confirmed. Connect the intended account and review this task before retrying."
    return result, "\n\nThe social draft receipt was not confirmed. This task was not marked done."


def harness_enabled(*, workspace_id: str | None = None, person_id: str | None = None) -> bool:
    """Live harness is on unless explicitly disabled or the LLM is offline.

    On a hosted (signed-caller) deployment, ``harness_research`` is a paid
    entitlement. Local-first stays free so offline dogfood still works.
    """
    import os

    if os.getenv("HARNESS_DISABLE", "").lower() in {"1", "true", "yes"}:
        return False
    if os.getenv("LLM_OFFLINE", "").lower() in {"1", "true", "yes"}:
        return False
    if os.getenv("COMPANY_OS_REQUIRE_SIGNED_CALLER", "").strip() in {"1", "true", "yes", "on"}:
        try:
            from app.billing.entitlements import require_entitlement
            from app.billing.entitlements import EntitlementDenied

            require_entitlement(
                "harness_research", workspace_id=workspace_id, person_id=person_id
            )
        except EntitlementDenied:
            return False
        except Exception:
            return False
    return True


def _harness_tools(speaker: dict | None) -> list[str]:
    tools = ["explore_web", "emit_signal", "summarize_runs", "list_agents", "check_policy"]
    role = str((speaker or {}).get("role") or "").lower()
    extras = {
        "architect": ["propose_code", "check_policy"],
        "builder": ["propose_code"],
        "reviewer": ["propose_code", "check_policy"],
        "copywriter": ["propose_config", "emit_signal"],
        "growth": ["propose_config", "emit_signal", "summarize_runs"],
        "analyst": ["summarize_runs", "list_feedback"],
        "support": ["ingest_feedback", "list_feedback"],
    }
    tools.extend(extras.get(role, []))
    seen: list[str] = []
    for name in tools:
        if name not in seen:
            seen.append(name)
    from app.tasks.skills import allowed_tools
    ceiling = allowed_tools(role or "researcher")
    return [name for name in seen if name in ceiling]


_STOP = frozenset("about their there which would could should software helps people using where these those "
                  "other every first from that this with your have been".split())


def _founder_first_name(venture_id: str) -> str | None:
    """The first name the greeting API stores on the account's Desk; tried for the workspace owner, then members."""
    try:
        from app.api.greeting_api import clean_first_name
        from app.db.models import Person, Venture, Workspace, WorkspaceMember

        with db()() as session:
            ven = session.get(Venture, venture_id)
            ws = session.get(Workspace, ven.workspace_id) if ven and ven.workspace_id else None
            people = [ws.owner_id] if ws else []
            if ws:
                people += [m for m in session.scalars(select(WorkspaceMember.person_id).where(
                    WorkspaceMember.workspace_id == ws.id)) if m not in people]
        for pid in people:
            with db()() as session:
                desk = session.scalars(select(Workspace).where(
                    Workspace.owner_id == pid, Workspace.kind == "desk")).first()
                settings = json.loads(desk.settings_json or "{}") if desk else {}
            if name := clean_first_name(settings.get("first_name")):
                return name
        with db()() as session:  # display_name is the "Founder" placeholder until a real name is known
            person = session.get(Person, people[0]) if people else None
            display = ((person.display_name if person else "") or "").split(" ")[0]
        return None if display.lower() == "founder" else clean_first_name(display)
    except Exception:  # noqa: BLE001
        return None


def _topic_words(ctx: dict) -> set[str]:
    blob = f"{ctx['problem']} {ctx['pitch']} {ctx['audience']}".lower()
    return {w[:5] for w in re.findall(r"[a-z]{5,}", blob) if w not in _STOP}


def drafts_on_topic(text: str, ctx: dict) -> bool:
    """A draft set is usable only if it shares the idea's own vocabulary and carries the landing link."""
    from app.autopilot import claims
    if (claims.violations(text, founder_words=ctx.get("founder_words", ""))
            or re.search(r"\[[^\]\n]*\]|\b(?:customer|audience|problem|product|pitch|idea|test|proposed|running|hypothesis)\s*:|My idea:", text, re.I)):
        return False
    if not ctx.get("request") and re.search(r"(?m)^(?!https?://)[a-z]|[.!?]\s+[a-z]", text.replace(ctx["founder"], "")):
        return False
    for greeting in re.findall(r"(?im)^(?:Hi|Hello|Dear)\b[^\n]*", text):
        if re.fullmatch(r"(?:Hi|Hello)(?: there)?,", greeting, re.I):
            continue
        named = re.fullmatch(r"(?:Hi|Hello|Dear)\s+([^,\n]+),", greeting, re.I)
        supplied_names = re.sub(r"\S+@\S+", "", ctx.get("request", ""))
        if not named or not re.search(r"(?<!\w)" + re.escape(named[1]) + r"(?!\w)", supplied_names, re.I):
            return False
    if any(url.rstrip(".,;!?") != ctx["link"] for url in re.findall(r"https?://[^\s<>\)]+", text)):
        return False
    bodies = re.split(r"(?im)^\s*Draft\s+\d+[^\n]*\n", text)[1:] or [text]
    if ctx["link"] and ctx["link"] not in text:
        return False
    words = _topic_words(ctx)
    problem_words = _topic_words({"problem": ctx["problem"], "pitch": "", "audience": ""})
    return all(ctx["pitch"].lower() in body.lower()
               or sum(word in body.lower() for word in words) >= min(3, len(words))
               or bool(problem_words) and sum(word in body.lower() for word in problem_words) >= min(3, len(problem_words))
               for body in bodies)


def _template_drafts(ctx: dict) -> str:
    from app.autopilot.outreach import product_phrase
    from app.intake.naming import recipient_name

    name, link, who = recipient_name(ctx["name"]), ctx["link"], ctx["founder"]
    more = f"\n\nYou can see the idea here: {link}" if link else ""
    opening = f"I'm exploring {product_phrase(ctx['pitch'], name)}."
    problem = f" {ctx['problem']}." if ctx["problem"].lower() != ctx["pitch"].lower() else ""
    return (
        f"Draft 1\nSubject: Quick question about {name}\n\nHi,\n\n{opening}{problem} "
        f"I'm learning how {ctx['audience']} handle this today. Would you be open to a short call?{more}\n\nThanks,\n{who}\n\n"
        f"Draft 2\nSubject: Your experience with {name}\n\nHi there,\n\n{opening}{problem} "
        f"What have you tried so far, and what is still difficult? A short reply would help.{more}\n\nThanks,\n{who}\n\n"
        f"Draft 3\nSubject: A conversation about {name}\n\nHi,\n\n{opening}{problem} "
        f"I'd value a recent example from your own experience: what happened, and how did you handle it?{more}\n\nThanks,\n{who}")


async def _review_drafts(venture_id: str, title: str, detail: str) -> str:
    """Outreach drafts for you to review, grounded in the venture's clean pitch, problem and landing page.

    Known unusable output or an offline model may use the brief's free template.
    An unconfirmed paid outcome must escape to the task's saved pause boundary.
    No recipient, claim, price or user count is invented; the founder chooses who gets what.
    """

    from app.autopilot.outreach import founder_signature, idea_phrase, idea_sentence, preserve_capitalization, product_description, saved_pitch
    from app.chat.intent import outreach_email_count
    from app.db.models import Venture
    from app.gtm.journey import _clean_problem, _icp
    from app.gtm.relationships.email_compliance import complete_review_text
    from app.ventures.state_brief import get_venture_state_brief
    from core.copy import no_dashes

    brief: dict = {}
    icp: dict = {}
    workspace_id = None
    try:
        with db()() as session:
            ven = session.get(Venture, venture_id)
            workspace_id = ven.workspace_id if ven else None
        brief = get_venture_state_brief(venture_id) or {}
        icp = _icp(venture_id)
    except Exception:  # noqa: BLE001 - the template below still works
        pass
    idea, site = brief.get("idea") or {}, brief.get("website") or {}
    recruitment_post = title == "Draft recruitment posts for me to review"
    interview_email = title == "Draft one interview email for me to review"
    request = re.sub(r"\nWrite [^\n]+ for review only\. Do not send, post or schedule anything\.\s*\Z", "",
        detail.removeprefix("Founder request: ")) if detail.startswith("Founder request: ") else ""
    requested_email = bool(request) and not recruitment_post
    count = outreach_email_count(request) if requested_email else 1 if interview_email else 3
    length = re.search(r"\b(\d+)\s*(?:-\s*)?words?\b", request, re.I)
    gtm = brief.get("gtm") or {}
    guidance = [idea_sentence(str(value), 240) for value in [gtm.get("bottleneck") or "", *(gtm.get("now_items") or [])]]
    try:  # the accepted plan's product name is the brand on the landing page
        from app.firstlook.service import plan_brief

        brand = plan_brief(venture_id).get("name")
    except Exception:  # noqa: BLE001
        brand = ""
    raw_idea = str(idea.get("raw_idea") or "")
    problem_notes = str(idea.get("problem") or icp.get("problem") or "")
    if not problem_notes and (match := re.search(r"\bProblem\s*:\s*(.*)", raw_idea, re.I | re.S)):
        problem_notes = match[1]
    pitch = next((clean for value in (saved_pitch(venture_id), idea.get("offer"), icp.get("offer"),
                                     raw_idea if not re.match(r"\s*(?:Customer|Audience)\s*:", raw_idea, re.I) else "",
                                     problem_notes)
                  if (clean := idea_phrase(preserve_capitalization(str(value or ""), raw_idea)))), "")
    if not pitch:
        return "I need your product description or the situation you want to improve before I can write useful outreach. Save it in your idea review, then ask me to draft again."
    from app.intake.naming import recipient_name

    name = recipient_name(idea_sentence(str(brand or idea.get("name") or (ven.name if workspace_id else "our project")), 80) or "our project", venture_id)
    pitch = product_description(pitch, venture_name=name) or name
    problem = idea_sentence(_clean_problem(problem_notes or pitch)) or idea_sentence(pitch)
    ctx = {
        "name": name,
        "audience": idea_sentence(str(idea.get("audience") or icp.get("who") or ""), 120) or "your intended customers",
        "pitch": pitch,
        "problem": problem[:1].upper() + problem[1:],
        "link": str((site.get("url") if site.get("live") else None) or site.get("waitlist_url") or site.get("url") or ""),
        "founder": founder_signature(venture_id),
    }
    format_instruction = (
        "Write recruitment posts for review, under 90 words each. Follow the requested number, defaulting to one. "
        "Include a concrete manual next action and requested channel; label unverified community suggestions as "
        "suggestions and do not claim their rules or audience were checked. Do not write email subjects or "
        "recipient greetings. Ask for a conversation or early-access interest; do not claim the product is "
        "ready or that pilots have succeeded. Number them Draft 1 to Draft 3."
        if recruitment_post else
        "Write exactly one non-sales interview email with a Subject line, under 90 words. Number it Draft 1. "
        "Preserve every founder privacy and no-product constraint. Ask only for anonymized verbal descriptions "
        "of the adult recipient's own general workflow. Do not ask to observe clients, parents or children, "
        "or request their records, screenshots, private messages or identifying/medical information. "
        "Do not claim the recipient matches an unverified customer segment. Do not include a signup link "
        "unless the founder explicitly asked for one."
        if interview_email else
        "Write three different short email drafts, each with a Subject line, under 90 words, naming the "
        "problem above and asking for a short conversation or early-access interest. Number them Draft 1 to Draft 3.")
    if requested_email:
        format_instruction = (
            f"Write exactly {count} email drafts with a Subject line, numbered from Draft 1. "
            + (f"Each email must contain about {length[1]} words, counting its greeting and signature but excluding "
               "the Subject line, Draft heading and compliance footer. " if length else "Keep each email short. ")
            + "Follow the founder's requested purpose, questions, audience and composition constraints instead of "
              "turning every request into customer discovery or a signup pitch. Preserve every privacy and "
              "no-product constraint. Never request identifying or medical information, private messages, records "
              "or screenshots. Do not ask to observe clients, parents or children. Do not include a signup link "
              "unless explicitly requested. Do not write compliance footers or source-disclosure, opt-out or "
              "sender-address lines. They are appended separately from verified data after composition and "
              "do not count toward the requested length.")
    from app.chat import assistant
    from app.autopilot import claims

    ctx["founder_words"] = claims.founder_words(venture_id)

    supplied = re.search(r"(?im)^[ \t]*Subject:[ \t]*(?P<subject>[^\r\n]+)\r?\n[ \t]*Body:[ \t]*(?:\r?\n)?(?P<body>[\s\S]+)", request)
    if requested_email and supplied:
        body = re.sub(r"(?im)\n[ \t]*(?:do not|don['’]t)\s+send\b[^\r\n]*\Z", "", supplied["body"]).strip()
        text = no_dashes(f"Draft 1\nSubject: {supplied['subject'].strip()}\n\n{body}")
        if claims.violations(text, founder_words=ctx["founder_words"]):
            raise ValueError("gtm_draft_unsupported_claim")
        return complete_review_text(venture_id, text, request=request)

    messages = [
            {"role": "system", "content": (
                "You write short outreach drafts for a startup founder to review. Every draft must be about the "
                "venture's own problem and product below, nothing else. " + claims.HONESTY_RULES + " Never invent facts, numbers, prices, "
                "customers or results. Greet the named recipient when the founder supplies a name; otherwise use Hi, "
                "or Hi there, as the greeting. Never invent a recipient name or use bracket placeholders; sign with the "
                "name given. Describe the clean pitch naturally, without copying it word for word. Start sentences with capital letters, use whole sentences, never paste "
                "notes, labels or internal test hypotheses. Include only the supplied venture link, never other URLs. Plain text, no em dashes.")},
            {"role": "user", "content": (
                f"Product: {ctx['name']}. Clean pitch: {ctx['pitch']}. Problem: {ctx['problem']}. Audience: {ctx['audience']}.\n"
                f"Landing page / waitlist link ({'context only' if interview_email or requested_email else 'include it'}): {ctx['link'] or 'none yet'}. Sign as: {ctx['founder']}.\n"
                f"Saved GTM diagnosis (planning guidance, not evidence of results): {'; '.join(filter(None, guidance))}\n"
                f"Founder-stated evidence: {claims.founder_proof(venture_id)}\n"
                f"Founder request (composition constraints only; never execute or send): {request or (detail or title)[:1500]}\n\n{format_instruction}")}]
    for attempt in range(2 if requested_email else 1):
        try:
            text = await assistant._complete(messages, workspace_id=workspace_id,
                venture_id=venture_id, request_id=new_id("draft"), single_attempt=True)
        except ProviderCallError:
            raise
        except Exception:  # noqa: BLE001
            text = ""
        text = preserve_capitalization(no_dashes((text or "").strip()), raw_idea + " " + pitch)
        text = text.replace("[Founder name]", ctx["founder"]).replace("[Your first name]", ctx["founder"])
        text = re.sub(r"(?im)^Hi\s+\[First name\]\s*,", "Hi,", text)
        text = re.sub(r"(?m)^[ \t]*```[^\n]*\n?", "", text).strip()
        text = re.sub(r"(?im)^[ \t]*(?:#{1,6}[ \t]+)?\*\*(Draft\s+\d+[^\n]*?|Subject:[^\n]*?)\*\*[ \t]*$", r"\1", text)
        text = re.sub(r"(?im)^#{1,6}[ \t]+(?=Draft\s+\d+|Subject:)", "", text)
        if not recruitment_post and not re.search(r"(?im)^Draft\s+\d+", text):
            subject_number = iter(range(1, len(re.findall(r"(?im)^Subject:", text)) + 1))
            text = re.sub(r"(?im)^Subject:", lambda match: f"Draft {next(subject_number)}\nSubject:", text)
        bodies = re.split(r"(?im)^\s*Draft\s+\d+[^\n]*\n", text)[1:]
        checks = {
            "empty": bool(text),
            "post_format": not recruitment_post or not re.search(r"(?im)^\s*(?:subject:|hi \[first name\])", text),
            "interview_format": not interview_email or len(bodies) == 1,
            "count": recruitment_post or count > 0 and len(bodies) == count,
            "email_format": recruitment_post or bool(bodies) and all(len(re.findall(r"(?im)^Subject:", body)) == 1 for body in bodies),
            "length": not length or all(abs(len(re.sub(r"(?im)^Subject:[^\n]*\n?", "", body).split()) - int(length[1])) <= max(5, int(length[1]) * 0.15) for body in bodies),
            "drafts_on_topic": drafts_on_topic(text, {**ctx, "request": request, "link": ""} if interview_email or requested_email else ctx),
        }
        failed = [name for name, passed in checks.items() if not passed]
        if not failed:
            return text if recruitment_post else complete_review_text(venture_id, text, request=request)
        logging.getLogger(__name__).warning("review_draft_rejected venture_id=%s attempt=%d checks=%s",
            venture_id, attempt + 1, ",".join(failed))
        if not requested_email or not text or attempt:
            break
        messages += [{"role": "assistant", "content": text}, {"role": "user", "content":
            "Rewrite the requested email drafts once, correcting these checks: " + ", ".join(failed) + ". "
            "Include the requested number of emails and a Subject line for each. Keep the requested length, "
            "purpose and named recipient. Describe the supplied problem naturally without inventing facts. "
            "Return only the drafts, without commentary or compliance footers."}]
    if requested_email or interview_email:
        from app.autopilot.outreach import _template

        # shortcut: plain templates preserve saved facts rather than exact stylistic constraints, use a grounded rewrite for exact style.
        named = re.search(r"\bto\s+([^\n@]+?)\s+at\s+[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", request, re.IGNORECASE)
        footer = complete_review_text(venture_id, "Draft 1\nSubject: " + ctx["name"], request=request).partition("\n\n")[2]
        if interview_email and not re.search(r"\bPlan:", request, re.IGNORECASE):
            problem = f" {ctx['problem']}." if ctx["problem"].lower() != ctx["pitch"].lower() else ""
            unbuilt = ("We have no product or validated results yet. "
                       if re.search(r"\b(?:no product|unbuilt|unvalidated concept)\b", detail, re.I) else "")
            subject = f"A short question about {ctx['name']}"
            body = (f"Hi,\n\nI'm exploring {ctx['name']}: {ctx['pitch']}.{problem} "
                    f"I'm learning how {ctx['audience']} handle this today. {unbuilt}"
                    "Would you be open to a short email reply or conversation about your own general workflow "
                    "and the last frustrating task? No sales pitch, observation, client or child details, records, "
                    "screenshots or private messages are needed.")
        else:
            subject, body = _template(ctx["pitch"], ctx["name"], ctx["problem"], venture_name=ctx["name"], request=request,
                word_budget=120 - len(footer.split()) - 1)
        body = body.replace("I'm building ", "I'm exploring ", 1)
        if named:
            body = body.replace("Hi,", f"Hi {named[1].strip()},", 1)
        body += f"\n\nThanks,\n{ctx['founder']}"
        text = "\n\n".join(f"Draft {number}\nSubject: {subject}\n\n{body}" for number in range(1, count + 1))
        if claims.violations(text, founder_words=ctx["founder_words"]):
            return ""
        return complete_review_text(venture_id, text, request=request)
    problem = f" {ctx['problem']}." if ctx["problem"].lower() != ctx["pitch"].lower() else ""
    if recruitment_post:
        link = f" Learn more: {ctx['link']}" if ctx["link"] else ""
        request = (detail or title).lower()
        ask = ("Would you tell me about your last no-show and how you handled it?"
               if re.search(r"last no[- ]show", request) else
               "Would you be open to a short conversation about how you handle this today?")
        count_match = re.search(r"\b(three|3|two|2|one|1)\s+(?:short\s+)?(?:recruitment\s+)?posts?\b", request)
        count = {"three": 3, "3": 3, "two": 2, "2": 2}.get(count_match[1], 1) if count_match else 1
        bodies = [
            f"I'm exploring {ctx['name']}: {ctx['pitch']}.{problem} {ask}{link}",
            f"Do you work with {ctx['audience']}? I'm exploring {ctx['name']}: {ctx['pitch']}.{problem} {ask}{link}",
            f"I'm exploring {ctx['name']}: {ctx['pitch']}.{problem} I'm learning how {ctx['audience']} handle this today. {ask}{link}",
        ]
        if "reddit" in request:
            channel = "Suggested channel: Reddit r/smallbusiness (audience fit and posting rules unverified)."
            step = "Open that community, review its rules, then choose Create Post and paste Draft 1 for your review."
        else:
            channel = "Suggested channel: your LinkedIn personal profile (audience fit unverified)."
            step = "Open your LinkedIn home feed, choose Start a post and paste Draft 1 for your review."
        return ("\n\n".join(f"Draft {i + 1}\n{body}" for i, body in enumerate(bodies[:count]))
                + f"\n\n{channel}\nManual next action: {step} Nothing has been posted or scheduled.")
    return complete_review_text(venture_id, _template_drafts(ctx))


@dataclass(frozen=True)
class ResearchNotes:
    """A saved note and source-owned page observations, never model prose alone."""
    text: str
    source_urls: tuple[str, ...] = ()
    observations: tuple[dict[str, str], ...] = ()

    @property
    def qualified(self) -> bool:
        return bool(self.source_urls)


def _research_url(url: Any) -> bool:
    if not isinstance(url, str) or len(url) > 2048 or any(ord(c) < 33 for c in url):
        return False
    try:
        parsed = urlsplit(url)
        return parsed.scheme in {"http", "https"} and bool(parsed.hostname) and not (parsed.username or parsed.password)
    except ValueError:
        return False


def _research_sources(result: Any) -> list[dict[str, str]]:
    """Only successfully read pages from the actual live explore result qualify.

    Search hits/snippets are discovery leads. A model's citations, an arbitrary
    successful tool or a copied brief do not establish a page observation.
    """
    if not isinstance(result, dict) or result.get("mode") != "live":
        return []
    hits, pages = result.get("hits"), result.get("pages")
    if not isinstance(hits, list) or not isinstance(pages, list):
        return []
    observed_urls = {row.get("url") for row in hits if isinstance(row, dict) and isinstance(row.get("url"), str)}
    qualified = []
    for page in pages[:10]:
        if not isinstance(page, dict):
            continue
        url, excerpt, chars = page.get("url"), page.get("excerpt"), page.get("chars")
        if (not _research_url(url) or url not in observed_urls
                or not isinstance(excerpt, str) or not excerpt.strip() or len(excerpt) > 20_000
                or type(chars) is not int or chars < len(excerpt) or page.get("error")):
            continue
        from app.tasks.skills import source_hash
        if source_hash(url) not in {source_hash(row["url"]) for row in qualified}:
            from hashlib import sha256
            qualified.append({"url": url, "excerpt": excerpt[:1800],
                "observed_at": utcnow().isoformat(), "excerpt_sha256": sha256(excerpt[:1800].encode()).hexdigest()})
    return qualified


def _research_hash(value) -> str:
    from hashlib import sha256
    return sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _research_binding(session, planned, run_id, step_id):
    """Bind source custody to the actual admitted handoff, not model prose."""
    from app.db.models import Job, Venture, Workspace
    from app.db.task_models import Task, TaskRun, TaskStep
    run, step = session.get(TaskRun, run_id), session.get(TaskStep, step_id)
    task = session.get(Task, run.task_id) if run else None
    venture = session.get(Venture, planned.venture_id) if planned else None
    workspace = session.get(Workspace, venture.workspace_id) if venture else None
    job = session.get(Job, step.job_id) if step and step.job_id else None
    detail = (planned.detail or planned.why_now or "") if planned else ""
    if (not planned or not task or not run or not step or not venture or not workspace or not job
            or step.run_id != run.id or step.skill != "task.run" or job.kind != "task.run"
            or (task.workspace_id, task.venture_id) != (venture.workspace_id, venture.id)
            or (job.workspace_id, job.venture_id) != (venture.workspace_id, venture.id)
            or planned.workspace_id not in {None, venture.workspace_id}
            or workspace.owner_id != task.owner_person_id
            or json.loads(job.payload_json).get("task_id") != planned.id
            or task.goal not in {planned.title, detail}):
        raise ValueError("research_task_binding_invalid")
    return {"workspace_id": venture.workspace_id, "venture_id": venture.id, "planned_task_id": planned.id,
        "job_id": job.id, "task_id": task.id, "run_id": run.id, "step_id": step.id,
        "goal_hash": _research_hash({"title": planned.title, "detail": detail}),
        "contract_hash": _research_hash({"goal": task.goal, "scope": json.loads(task.scope_json),
            "done_test": json.loads(task.done_test_json), "step_check": json.loads(step.done_test_json)})}


def _research_mode(session, binding, count):
    from app.db.task_models import Task, TaskStep
    from app.tasks.checks import passes, validate
    task, step = session.get(Task, binding["task_id"]), session.get(TaskStep, binding["step_id"])
    whole, check = json.loads(task.done_test_json), json.loads(step.done_test_json)
    validate(whole, whole_task=True)
    validate(check)
    evidence = [{"source_count": count}]
    if count < 1 or check.get("type") == "rubric" or not passes(check, evidence):
        return None
    if whole.get("type") == "rubric":
        return "rubric_prepared"
    counts = [selected for selected in (check, whole) if isinstance(selected.get("spec"), dict)
        and selected["spec"].get("predicate") == "at_least" and selected["spec"].get("metric") == "source_count"]
    if counts:
        return "count_verified" if all(passes(selected, evidence) for selected in counts) else None
    return None  # Legacy/open-ended coverage is not proven by page custody.


def _observed_metadata(observations):
    from datetime import datetime
    from app.tasks.skills import source_hash
    metadata = {}
    for item in observations:
        if (not isinstance(item, dict) or not _research_url(item.get("url"))
                or not re.fullmatch(r"[a-f0-9]{64}", str(item.get("excerpt_sha256") or ""))):
            raise ValueError("research_source_receipt_invalid")
        moment = datetime.fromisoformat(item["observed_at"])
        if moment.tzinfo is None:
            raise ValueError("research_source_receipt_invalid")
        metadata.setdefault(source_hash(item["url"]), {key: item[key] for key in ("url", "observed_at", "excerpt_sha256")})
    return list(metadata.values())


def _research_preparation(venture_id, task_id, title, detail, research):
    from app.tasks.usage import context
    current = context()
    if not current or not isinstance(research, ResearchNotes) or not research.observations:
        return None
    with db()() as session:
        planned = session.get(PlannedTask, task_id)
        if not planned or planned.venture_id != venture_id or planned.title != title or (planned.detail or planned.why_now or "") != detail:
            return None
        from app.db.task_models import Task, TaskRun
        run = session.get(TaskRun, current[0])
        task = session.get(Task, run.task_id) if run else None
        if task and task.goal not in {title, detail}:
            return None  # Existing auto-projected Work jobs retain their legacy coverage gate.
        from app.tasks.service import TERMINAL, WAITING
        if not task or not run or task.state in TERMINAL | WAITING or run.state in TERMINAL | WAITING:
            return None
        binding = _research_binding(session, planned, *current)
        sources = _observed_metadata(research.observations)
        if tuple(item["url"] for item in sources) != research.source_urls:
            raise ValueError("research_source_receipt_invalid")
        mode = _research_mode(session, binding, len(sources))
        return {"schema_version": 1, "capture": "explore_web.pages_v1", **binding,
            "mode": mode, "source_count": len(sources), "observed_sources": sources} if mode else None


def verified_research_receipt(session, planned, note, job_id):
    """Read-only receipt check shared by worker completion and rubric finalization."""
    from hashlib import sha256
    receipt = json.loads(note.provenance_json).get("research_receipt") if note else None
    if not isinstance(receipt, dict):
        raise ValueError("research_source_receipt_missing")
    binding = _research_binding(session, planned, receipt["run_id"], receipt["step_id"])
    sources = _observed_metadata(receipt["observed_sources"])
    if (type(receipt.get("schema_version")) is not int or receipt["schema_version"] != 1
            or receipt.get("capture") != "explore_web.pages_v1" or receipt.get("job_id") != job_id
            or any(receipt.get(key) != value for key, value in binding.items())
            or not note or (note.id, note.venture_id, note.kind, note.title) != (
                "task-note:" + planned.id, planned.venture_id, "task_note", planned.title)
            or receipt.get("body_sha256") != sha256((note.body_md or "").encode()).hexdigest()
            or receipt.get("receipt_sha256") != _research_hash({key: value for key, value in receipt.items() if key != "receipt_sha256"})
            or receipt.get("source_count") != len(sources) or type(receipt.get("source_count")) is not int
            or sources != receipt["observed_sources"] or receipt.get("mode") != _research_mode(session, binding, len(sources))):
        raise ValueError("research_source_receipt_invalid")
    return receipt


def finalize_research_for_run(session, run_id):
    """Root calls after its whole-run rubric passes; source receipt alone cannot finalize."""
    from app.db.models import VentureDocument
    from app.db.task_models import TaskRun, TaskStep
    run = session.get(TaskRun, run_id)
    if not run or run.state != "completed":
        return 0
    finalized = 0
    for step in session.scalars(select(TaskStep).where(TaskStep.run_id == run_id, TaskStep.skill == "task.run")):
        if step.state != "completed" or not step.job_id:
            continue
        task_id = json.loads(step.inputs_json).get("task_id")
        planned = session.get(PlannedTask, task_id) if task_id else None
        note = session.get(VentureDocument, "task-note:" + task_id) if task_id else None
        if not note or "research_receipt" not in json.loads(note.provenance_json or "{}"):
            continue
        receipt = verified_research_receipt(session, planned, note, step.job_id)
        if receipt["mode"] == "rubric_prepared" and planned.status == "open":
            _close_done(session, planned)
            finalized += 1
    return finalized


def _recover_research_receipt(venture_id, task_id):
    """A committed source receipt survives the separate legacy completion write."""
    from app.db.models import VentureDocument
    from app.db.session import serialize_writes
    from app.db.task_models import Task, TaskRun, TaskStep
    from app.tasks import service, usage
    if not (current := usage.context()):
        return None
    with db()() as session:
        serialize_writes(session, key="task-note:" + task_id)
        note = session.get(VentureDocument, "task-note:" + task_id)
        if not note:
            return None
        try:
            provenance = json.loads(note.provenance_json or "{}")
            valid_provenance = isinstance(provenance, dict)
        except (TypeError, ValueError):
            provenance, valid_provenance = {}, False
        if valid_provenance and "research_receipt" not in provenance:
            return None
        run = session.get(TaskRun, current[0], with_for_update=True)
        step = session.get(TaskStep, current[1], with_for_update=True)
        task = session.get(Task, run.task_id, with_for_update=True) if run else None
        planned = session.get(PlannedTask, task_id, with_for_update=True)
        if (not task or not run or not step or not planned or step.run_id != run.id or task.venture_id != venture_id
                or planned.venture_id != venture_id or planned.workspace_id not in {None, task.workspace_id}):
            raise PermissionError("research_task_scope_invalid")
        slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in planned.title.lower())[:48] or "task"
        artifact = data_root() / "ventures" / venture_id / "repo" / "tasks" / (slug + ".md")
        if task.state in service.TERMINAL | service.WAITING or run.state in service.TERMINAL | service.WAITING:
            return {"id": task_id, "status": "blocked", "artifact": str(artifact), "title": planned.title}
        try:
            receipt = verified_research_receipt(session, planned, note, step.job_id)
        except (KeyError, TypeError, ValueError):
            service.block(session, task, {"class": "needs_input", "reason": "The saved research receipt no longer matches this task.",
                "unblock_action": "Review the saved sources before creating a fresh research request."}, run=run, step=step)
            _park_incomplete(session, planned, "incomplete:research_receipt_invalid")
            session.commit()
            return {"id": task_id, "status": "blocked", "artifact": str(artifact), "title": planned.title}
        if receipt["mode"] == "count_verified":
            _close_done(session, planned)
        else:
            _park_incomplete(session, planned, "incomplete:research_judgment_pending")
        session.commit()
        return {"id": task_id, "status": "done" if receipt["mode"] == "count_verified" else "prepared",
            "artifact": str(artifact), "title": planned.title}


async def _research_notes(title: str, detail: str, *, workspace_id=None, venture_id=None) -> ResearchNotes:
    from app.tasks.progress import ResearchStopped, before_search, record_tool
    from app.tasks.usage import before_tool
    query = title if len(title) > 12 else f"{title} {detail}".strip()
    scope = {"workspace_id": workspace_id, "venture_id": venture_id}
    try:
        from app.research.explore import explore_web

        before_search(query, **scope)
        before_tool()
        result = await explore_web(query)
    except Exception as exc:  # noqa: BLE001 - preserve the brief without promoting failure to completion
        try:
            record_tool("explore_web", {"query": query}, None, source_count=0, error=exc, **scope)
        except ResearchStopped:
            pass
        return ResearchNotes(f"Live search failed. Research is incomplete. Notes from the brief:\n\n{detail}")
    sources = _research_sources(result)
    try:
        record_tool("explore_web", {"query": query}, {"ok": True, "result": result}, source_count=len(sources), **scope)
    except ResearchStopped:
        return ResearchNotes(f"Live research needs review before it can continue. Notes from the brief:\n\n{detail}")
    if not isinstance(result, dict):
        return ResearchNotes(f"Live research returned no qualified sources. Notes from the brief:\n\n{detail}")
    summary = result.get("summary")
    lines = [summary if isinstance(summary, str) and summary else "Search returned a result.", ""]
    pages = result.get("pages") if isinstance(result.get("pages"), list) else []
    for page in pages[:10]:
        if not isinstance(page, dict):
            continue
        url = page.get("url") or ""
        if not _research_url(url):
            continue
        excerpt = page.get("excerpt") or page.get("snippet") or ""
        excerpt = excerpt.strip()[:1800] if isinstance(excerpt, str) else ""
        heading = page.get("title") or url
        lines.append(f"## {heading}\n\n{url}\n\n{excerpt}\n")
    if len(lines) <= 2:
        hits = result.get("hits") if isinstance(result.get("hits"), list) else []
        for hit in hits[:10]:
            if isinstance(hit, dict) and _research_url(hit.get("url")):
                lines.append(f"- {hit.get('title') or hit.get('url')}: {hit.get('url')}")
        if detail:
            lines.extend(["", detail])
    if not sources:
        lines.append("Research is incomplete: no successfully read live source pages were qualified.")
    return ResearchNotes("\n".join(lines).strip(), tuple(row["url"] for row in sources), tuple(_observed_metadata(sources)))


async def _harness_research(
    *,
    venture_id: str,
    title: str,
    detail: str,
    speaker: dict | None,
    workspace_id: str | None,
    llm: Any | None = None,
    warden: Any | None = None,
) -> ResearchNotes | None:
    """Think → search → note. None means fall back to a single explore_web."""
    if not harness_enabled(workspace_id=workspace_id) and llm is None:
        return None
    try:
        from harness import Inbox
    except ModuleNotFoundError as exc:
        if exc.name != "harness":
            raise
        return None
    from llm_gateway.client import LLMClient

    from app.munder.control import take_steer
    from app.runtime.agent_runtime import AgentRuntime
    from app.runtime.toolbus import ToolBus
    from app.runtime.live_turns import register, unregister
    from app.state import MEM

    client = llm
    if client is None:
        gateway = LLMClient()
        if gateway.offline:
            return None

        async def _llm(messages, **kwargs):
            from app.credits.metered import llm as metered_llm
            from llm_gateway.client import ChatResult
            from llm_gateway.messages import assistant_message_preserving_reasoning
            response = await metered_llm(
                messages, purpose=str(kwargs.get("purpose") or "harness.turn"),
                tier=str(kwargs.get("tier") or "flash"), tools=kwargs.get("tools"),
                workspace_id=workspace_id, venture_id=venture_id, max_tokens=4096,
            )
            assistant = assistant_message_preserving_reasoning(response)
            return ChatResult(content=assistant.get("content"), raw=response,
                              assistant_message=assistant, tool_calls=assistant.get("tool_calls") or [])

        client = _llm

    base_client = client

    async def research_client(messages, **kwargs):
        from app.tasks.progress import reflect_if_due
        query = await reflect_if_due(workspace_id=workspace_id, venture_id=venture_id)
        if query:
            messages = [*messages, {"role": "system", "content": (
                "Continue the original research goal with the same permitted tools. "
                "The next explore_web call must use this different public search query: " + query)}]
        return await base_client(messages, **kwargs)

    client = research_client

    inbox = Inbox()
    if steer_note := take_steer(venture_id):
        inbox.steer(str(steer_note))
    charter = MEM.get(f"charter:{venture_id}") or {}
    facts = {
        "venture": venture_id,
        "task": title,
        "charter": (charter.get("one_liner") or charter.get("name") or "")[:400],
    }

    async def _drain_steers() -> None:
        note = take_steer(venture_id)
        if note:
            inbox.steer(str(note))

    source_observations = []
    max_steps = 6
    from app.tasks.usage import context
    if current := context():
        from app.db.task_models import Task, TaskRun, TaskStep
        from app.tasks.budgets import effective_limits
        with db()() as session:
            run, step = session.get(TaskRun, current[0]), session.get(TaskStep, current[1])
            task = session.get(Task, run.task_id) if run else None
            if not task or not step or step.run_id != run.id or (task.workspace_id, task.venture_id) != (workspace_id, venture_id):
                raise PermissionError("research_task_scope_invalid")
            checks = [json.loads(task.done_test_json), json.loads(step.done_test_json)]
            requested = max((check["spec"]["count"] for check in checks if isinstance(check.get("spec"), dict)
                and check["spec"].get("metric") == "source_count" and type(check["spec"].get("count")) is int), default=0)
            remaining = max(0, effective_limits(task, run).tool_calls - run.tool_calls)
            max_steps = min(12, max(6, (requested + 2) // 3 + 1), remaining + 1)

    class ResearchBus(ToolBus):
        async def call(self, tool, args, **kwargs):
            from app.tasks.progress import before_search, record_tool
            if tool not in _harness_tools(speaker):
                record_tool(tool, args, None, source_count=len({row["url"] for row in source_observations}),
                    workspace_id=workspace_id, venture_id=venture_id, error=PermissionError("task_tool_not_allowed"))
                raise PermissionError("task_tool_not_allowed")
            if tool == "explore_web":
                before_search(args.get("query") or args.get("goal"), workspace_id=workspace_id, venture_id=venture_id)
            from app.tasks.usage import before_tool
            before_tool()
            try:
                response = await super().call(tool, args, **kwargs)
            except Exception as exc:
                record_tool(tool, args, None, source_count=len({row["url"] for row in source_observations}),
                    workspace_id=workspace_id, venture_id=venture_id, error=exc)
                raise
            # Capture at the source-owned named tool boundary. The model's
            # tool_results/final answer cannot supply or alter this evidence.
            if tool == "explore_web" and isinstance(response, dict) and response.get("ok") is True:
                from app.tasks.skills import source_hash
                known = {source_hash(row["url"]) for row in source_observations}
                for observed in _research_sources(response.get("result")):
                    identity = source_hash(observed["url"])
                    if identity not in known and len(source_observations) < 30:
                        source_observations.append(observed)
                        known.add(identity)
            record_tool(tool, args, response, source_count=len({row["url"] for row in source_observations}),
                workspace_id=workspace_id, venture_id=venture_id)
            return response

    register(venture_id, inbox)
    try:
        rt = AgentRuntime(llm=client, warden=warden, bus=ResearchBus(warden=warden))
        result = await rt.run(
            venture_id=venture_id,
            agent_id=str((speaker or {}).get("id") or "researcher"),
            goal=f"{title}\n\n{detail}".strip(),
            system_prompt=(
                f"You are {(speaker or {}).get('name') or 'the researcher'}. "
                f"{(speaker or {}).get('title') or 'Research'}. "
                "Search before you claim a fact. Cite URLs from tool results. "
                "End with a short founder-facing brief and an explicit gaps list."
            ),
            tools=_harness_tools(speaker),
            max_steps=max_steps,
            live=True,
            workspace_id=workspace_id,
            facts=facts,
            inbox=inbox,
            on_pre_step=_drain_steers,
        )
    except Exception:  # noqa: BLE001
        return ResearchNotes(f"Harness research failed. Research is incomplete. Notes from the brief:\n\n{detail}")
    finally:
        unregister(venture_id)

    completed = result.status == "finished" and result.reason == "completed"
    # Harness error text may contain raw provider exceptions; preserve the
    # reviewed brief and observed pages, rather than exporting that exception.
    text = ((result.final_text or "").strip() if completed else
            f"The research harness did not finish. Notes from the brief:\n\n{detail}")
    if not text:
        return ResearchNotes(f"Harness research did not produce a completed source brief. Notes from the brief:\n\n{detail}")
    used_search = bool(source_observations) and completed
    header = "Harness research (searched, then wrote this note)." if used_search else "Harness research."
    try:
        from app.analytics.events import emit

        emit(
            "harness.research",
            workspace_id=workspace_id,
            venture_id=venture_id,
            props={"used_search": used_search},
        )
    except Exception:
        pass
    unique = {}
    for row in source_observations:
        unique.setdefault(row["url"], row)
    unique_observations = list(unique.values())
    if unique_observations:
        text += "\n\n## Observed live sources\n\n" + "\n\n".join(
            f"{row['url']}\n{row['excerpt']}" for row in unique_observations)
    if not used_search:
        text += "\n\nResearch is incomplete: model prose did not establish any successfully read live source pages."
    return ResearchNotes(f"{header}\n\n{text}", tuple(row["url"] for row in unique_observations) if used_search else (),
        tuple(_observed_metadata(unique_observations)) if used_search else ())


async def _ship_first_look(venture_id: str, title: str, detail: str) -> tuple[bool, str]:
    """Update the served first-look HTML. Search notes do not count."""
    from app.db.models import Venture
    from app.firstlook.versions import get_latest

    factory = db()
    with factory() as session:
        ven = session.get(Venture, venture_id)
        if not ven or not ven.workspace_id:
            return False, "No workspace to serve a first-look page yet."
        wid = ven.workspace_id
        idea = (ven.raw_idea or title or "").strip()
        name = ven.name
    from app.core.local_mode import is_local
    if is_local():
        from app.firstlook.service import FirstLookError, generate_first_look
        try:
            await generate_first_look(wid, idea, venture_id=venture_id, venture_name=name,
                stage="refined", instruction=detail or title)
        except FirstLookError as exc:
            return False, "\n\n" + exc.message
        return False, "\n\nYour coding tool is working on your requested page changes. Open Builds to check its progress before completing this task."
    before = get_latest(wid)
    if before is None:  # template pages are retired: never create one; Claude Code / Codex builds the page
        return False, "\n\nNo page was created: build your landing page in Website with Claude Code or Codex."
    before_sha = before.sha256
    instruction = detail or title
    if len(instruction) > 600:
        return False, "Your rewrite instructions need to be 600 characters or fewer. Shorten your request and try again. No rewrite started and nothing was charged."
    from app.state import MEM

    charter = MEM.get(f"charter:{venture_id}") or {}
    try:
        from app.firstlook.service import generate_first_look, refine_from_charter

        if charter:
            view = await refine_from_charter(
                wid,
                idea,
                charter,
                instruction=instruction,
                venture_id=venture_id,
                venture_name=name,
            )
        else:
            view = await generate_first_look(
                wid,
                idea or detail,
                instruction=instruction,
                venture_id=venture_id,
                venture_name=name,
                stage="refined",
            )
    except Exception as exc:  # noqa: BLE001
        return False, f"Could not update the served first-look HTML ({exc})."
    if view.fallback:
        return False, "\n\nA fallback draft was saved, but your requested changes were not applied. Review the saved draft before trying again."
    digest = getattr(view, "sha256", None)
    changed = bool(digest) and digest != before_sha
    version = getattr(view, "version", None)
    if changed:
        return True, f"\n\nServed first-look HTML is now version {version}."
    return False, "\n\nFirst-look HTML did not change, this landing/copy task is not done."


async def _run_prototype(venture_id: str, title: str, detail: str, repo: Path, task_id: str) -> tuple[bool, str]:
    from app.core.local_mode import is_local

    if is_local():
        return False, "\n\nNo coding work started. Ask in your venture chat to build with your connected tool, then review its files and checks before marking this task done."
    try:
        from coding_agents.dispatcher import dispatch

        result = await dispatch(
            prompt=f"{title}\n\n{detail}",
            repo=repo,
            branch=f"wo/task-{task_id[:8]}",
            kind="code",
            mode="auto",
            timeout_s=120,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"\n\nDispatcher unavailable ({exc}); prototype is not done."
    changed = list(result.changed_files or [])
    if result.ok and changed:
        return True, (
            f"\n\nCoding dispatcher `{result.session_id or 'local'}` "
            f"touched {len(changed)} file(s)."
        )
    if result.error:
        return False, f"\n\nDispatcher skipped ({result.error}); prototype is not done."
    return False, "\n\nDispatcher wrote no files; prototype is not done."


def _requires_task_approval(title: str, detail: str, tags: list) -> bool:
    """Generic social posts are simulated drafts; other effects still need review."""
    from app.tasks.contracts import requires_reviewed_action

    if work_kind(title, tags, detail) == "outreach":
        title, detail = (re.sub(r"\b(?:post|publish|posting|publishing|send|sending|sent)\b", "draft", value, flags=re.I)
                         for value in (title, detail))
    return requires_reviewed_action(title, detail)


def _is_local_build_task(task: dict | None) -> bool:
    from app.core.local_mode import is_local
    from app.chat.steer import is_review_draft

    if not is_local() or not task:
        return False
    title, detail, tags = task.get("title", ""), task.get("detail") or "", task.get("tags") or []
    if _local_brief_only(title) or is_review_draft(title, detail) or _is_private_email(title, detail):
        return False
    return (work_kind(title, tags, detail) in {"site", "prototype"}
        or _is_site_copy(title, detail, tags)
        or any(tag in {"capability:firstlook.landing", "capability:build.run", "capability:coding.subscription_static",
                       "capability:coding.subscription_product_source"} for tag in tags)
        or bool(re.search(r"\b(?:create|write|implement|develop)\s+(?:(?:a|an|the|this|your|custom|small|local)\s+)*"
                          r"(?:scraper|script|tool)\b|\b(?:fix|repair)\b[^.!?\n]*\bbug\b|^\s*bug[- ]fix\b",
                          f"{title} {detail}", re.I)))


def _request_local_coding(venture_id: str, instruction: str, *, plan_task_id=None, caller=None) -> dict:
    from app.builder.ctl import request_local_build
    from fastapi import HTTPException

    try:
        return request_local_build(venture_id, instruction, repair=True, plan_task_id=plan_task_id, caller=caller)
    except HTTPException as exc:
        if isinstance(exc.detail, dict) and exc.detail.get("code") == "insufficient_credits":
            from app.credits.errors import LIMIT_MESSAGE

            exc.detail = {**exc.detail, "message": LIMIT_MESSAGE}
        elif isinstance(exc.detail, dict) and exc.detail.get("code") in {
                "build_tool_needs_setup", "coding_tool_not_connected", "device_coding_subscription_required"} and (
                not exc.detail.get("message") or "Connections > Coding tools" in exc.detail["message"]):
            exc.detail = {**exc.detail, "message": "No build started; open Settings > Keys and connections > Coding tools, "
                "connect your tool, sign in with your subscription, allow Charlie to use it, then retry your request."}
        raise


def _start_local_build_task(venture_id: str, task: dict) -> dict:
    from app.admin.authn import CallerContext
    from app.autopilot.authority import founder_scope
    from app.build import device_orders
    from app.munder.control import tool_decision
    from app.planning.store import get_task, task_was_cancelled

    if tool_decision(venture_id, "code").get("deny"):
        raise TaskRunError(409, "code_denied", "Your coding work is paused; open Work to review this task and resume it before running it.")
    current = get_task(venture_id, task["id"])
    scope_fields = ("title", "detail", "source", "tags")
    if (task_was_cancelled(task["id"]) or not current or current["status"] not in {"open", "running"}
            or any(current.get(field) != task.get(field) for field in scope_fields)):
        raise TaskRunError(409, "task_changed", "Your saved work changed; open Work to review the current request before running it.")
    tags = task.get("tags") or []
    plan_task_id = next((tag.removeprefix("plan_task:") for tag in tags if tag.startswith("plan_task:")), None)
    instruction = f"Saved work: {task['id']}\nSource: {task.get('source') or ''}\n\n{task['title']}\n\n{task.get('detail') or ''}"
    with founder_scope(venture_id, autonomous=True) as scope:
        caller = CallerContext(sub=scope.actor, email="", role="user", path="", method="POST", exp=0, nonce="")
        order = _request_local_coding(venture_id, instruction, plan_task_id=plan_task_id, caller=caller)
        current = get_task(venture_id, task["id"])
        if (task_was_cancelled(task["id"]) or not current or current["status"] not in {"open", "running"}
                or any(current.get(field) != task.get(field) for field in scope_fields)):
            device_orders.emergency_stop(venture_id, caller, order["id"])
            return {"id": task["id"], "title": task["title"], "status": "cancelled",
                    "device_work_order_id": order["id"], "review_path": order["review_path"]}
        from app.autopilot import receipts
        from app.db.models import VentureDocument

        with db()() as session:
            document = session.get(VentureDocument, order["id"])
            if (document and document.venture_id == venture_id and document.kind == device_orders.KIND
                    and json.loads(document.provenance_json or "{}").get("state") in {"approved", "claimed", "waiting_for_device"}):
                receipts.saved_document(document.id, document.kind, document.body_md, provenance=document.provenance_json)
    return {"id": task["id"], "title": task["title"], "status": "queued", "queued": True,
            "device_work_order_id": order["id"], "review_path": order["review_path"]}


def _precheck(venture_id: str, task_id: str, *, requested_build: bool = False) -> None:
    """Refuse work that must not start: paused venture, out-of-scope or metered plan tasks."""
    from app.planning.store import get_task
    from app.core.local_mode import is_local
    from app.ventures.lifecycle import is_paused

    factory = db()
    if is_paused(venture_id):
        raise TaskRunError(
            409,
            "venture_paused",
            "This venture is paused. Resume it before running tasks.",
        )

    # Approved plan tasks that bind to a metered executor must enter through
    # that executor's quote/approval workflow. The generic runner has no
    # authority to reserve credits or create a hosted build receipt.
    task_snapshot = get_task(venture_id, task_id)
    if task_snapshot:
        title, detail, tags = task_snapshot.get("title", ""), task_snapshot.get("detail") or "", task_snapshot.get("tags") or []
        if _requires_task_approval(title, detail, tags) and work_kind(title, tags, detail) != "site":
            raise TaskRunError(409, "task_approval_required", "Review the exact delivery, purchase or deletion in its approved workflow before running this task.")
        if is_local() and requested_build:
            from app.munder.adjudicate import classify_work

            if classify_work(title, detail, tags).kind == "critical":
                raise TaskRunError(409, "task_approval_required",
                    "This task changes data, spending or your reviewed scope; open Work to review that exact action before running it.")
    if task_snapshot and str(task_snapshot.get("last_error") or "").startswith("needs_you:review_draft_"):
        raise TaskRunError(409, str(task_snapshot["last_error"]).removeprefix("needs_you:"),
                           "Review the saved provider outcome in Work before continuing this draft.")
    if task_snapshot and str(task_snapshot.get("source") or "").startswith("approved_plan:"):
        snapshot_tags = task_snapshot.get("tags") or []
        plan_id = str(task_snapshot["source"]).removeprefix("approved_plan:")
        plan_task_id = next((tag.removeprefix("plan_task:") for tag in snapshot_tags
                             if tag.startswith("plan_task:")), None)
        if plan_task_id:
            from app.db.supymem_models import ExecutionPlan

            with factory() as session:
                source_plan = session.get(ExecutionPlan, plan_id)
                if source_plan and source_plan.venture_id == venture_id:
                    scope = (json.loads(source_plan.structured).get("scope_details") or {}).get(plan_task_id, {})
                    if scope.get("scope") == "out":
                        raise TaskRunError(
                            409, "plan_task_unavailable",
                            "This task is unavailable in the approved plan. Review and revise that step in Execution.",
                        )
        if "capability:build.run" in snapshot_tags and not (is_local() and requested_build):
            raise TaskRunError(
                409,
                "dedicated_workflow_required",
                ("Ask in your venture chat to build with your connected coding tool, then open Build activity to watch or stop it."
                 if is_local() else "Review this build's estimate and approve its budget in Builds before it starts."),
            )



async def execute_planned_task(
    venture_id: str,
    task_id: str,
    *,
    already_claimed: bool = False,
    post_completion: bool = True,
) -> dict:
    """Execute one planned task. Safe to call from the API or the autopilot driver.

    Checkout is atomic (Paperclip ``issues.checkout``): open → running only if
    the row is still open. A second caller gets ``already_running`` instead of
    double-work.
    """
    factory = db()
    from app.planning.store import get_task
    from app.paperclip.checkout import claim_task
    from app.paperclip.goals import ancestry_markdown

    from app.core.local_mode import is_local

    saved_task = get_task(venture_id, task_id) if is_local() else None
    local_build = _is_local_build_task(saved_task)
    _precheck(venture_id, task_id, requested_build=local_build)
    if local_build and (saved_task["status"] == "open" or already_claimed and saved_task["status"] == "running"):
        return _start_local_build_task(venture_id, saved_task)

    if recovered := _recover_research_receipt(venture_id, task_id):
        return recovered

    if already_claimed:
        claimed = get_task(venture_id, task_id)
        if not claimed or claimed.get("status") != "running":
            raise TaskRunError(409, "checkout_conflict", "Task is not checked out.")
    else:
        claimed = claim_task(venture_id, task_id)
        if claimed is None:
            existing = get_task(venture_id, task_id)
            if not existing:
                raise TaskRunError(404, "not_found", "Task not found.")
            if existing.get("status") == "done":
                return {
                    "id": existing["id"],
                    "status": "done",
                    "already_done": True,
                    "title": existing.get("title"),
                }
            if existing.get("status") == "running":
                return {
                    "id": existing["id"],
                    "status": "running",
                    "already_running": True,
                    "title": existing.get("title"),
                }
            raise TaskRunError(409, "checkout_conflict", "Could not check out this task.")

    with factory() as session:
        row = session.get(PlannedTask, task_id)
        if not row or row.venture_id != venture_id:
            raise TaskRunError(404, "not_found", "Task not found.")
        title = row.title
        detail = row.detail or row.why_now or ""
        why_now = row.why_now or ""
        try:
            tags = json.loads(row.tags_json or "[]")
        except json.JSONDecodeError:
            tags = []
        if _requires_task_approval(title, detail, tags) and work_kind(title, tags, detail) != "site":
            raise TaskRunError(409, "task_approval_required", "Review the exact action in its approved workflow before running this task.")

    owner = decode_assignee(tags)
    from app.org.characters import owner_for_work
    from app.org.summary import resolve_org

    org = resolve_org(venture_id)
    if owner.get("owner_name"):
        speaker = {
            "id": owner.get("owner_id") or "",
            "name": owner.get("owner_name"),
            "title": owner.get("owner_role") or "teammate",
            "role": owner.get("owner_role") or "",
        }
    else:
        speaker = owner_for_work(title, tags, org.get("agents") or [])

    _mark(speaker, venture_id, working=f"Working on {title}")

    from app.munder.breaker import record_tool_use
    from app.munder.control import take_steer, tool_decision

    record_tool_use(venture_id, "task.run", {"title": title})
    steer_note = take_steer(venture_id)

    repo = _venture_repo(venture_id)
    slug = "".join(c if c.isalnum() or c in "-_" else "-" for c in title.lower())[:48] or "task"
    artifact = repo / "tasks" / f"{slug}.md"
    extra = ""
    who = speaker.get("name") if speaker else "the office"

    gate = _founder_gate(title, tags, venture_id, detail)
    if gate is None and _requires_task_approval(title, detail, tags):
        gate = {"provider": "decision", "title": "Review this action before it runs",
                "why": "Review the exact delivery, purchase or deletion in its approved workflow. Nothing was executed."}
    if gate and not _is_private_email(title, detail):
        from app.concierge.human import ensure_human_task

        human = ensure_human_task(
            venture_id,
            title=str(gate["title"]),
            why=str(gate["why"]),
            provider=str(gate["provider"]),
            category="decision" if gate["provider"] in {"decision", "payments"} else "credential",
        )
        if gate["provider"] not in {"decision", "payments"}:
            from app.secrets.need import request_for_provider

            request_for_provider(
                str(gate["provider"]),
                venture_id=venture_id,
                reason=str(gate["why"]),
            )
        if gate["provider"] == "decision":
            from app.state import MEM
            from app.munder.ledger import ask_human

            MEM[f"munder_decision_card:{venture_id}"] = task_id
            ask_human(venture_id, task_id, str(gate["why"]))
        _write_artifact(
            artifact,
            title,
            f"Status: waiting on you\n\n{gate['why']}\n\n{detail}",
            speaker,
            venture_id=venture_id, task_id=task_id,
        )
        _chat(
            venture_id,
            task_id,
            f"I need **{gate['title']}** before I can finish **{title}**. "
            f"{gate['why']} I saved this requirement in Needs you. "
            + ("Choose the merchant before any credential setup; no payment credential was requested."
               if gate["provider"] == "payments" else
               "Review the saved decision before this work continues."
               if gate["provider"] == "decision" else
               "Use Connections for supported secure credential fields, never enter credentials in chat."),
            speaker,
            kind="needs_you",
            actions=with_explain({}, "founder_only_work" if gate["provider"] == "decision"
                                 else "connection_missing")["actions"],
        )
        with factory() as session:
            row = session.get(PlannedTask, task_id)
            if not row or row.venture_id != venture_id:
                raise TaskRunError(404, "not_found", "Task not found.")
            row.status = "open"
            row.runnable = 0
            row.last_error = f"needs_you:{gate['provider']}"
            row.updated_at = utcnow()
            session.commit()
        _mark(speaker, venture_id, working=None)
        return {
            "id": task_id,
            "status": "needs_you",
            "artifact": str(artifact),
            "title": title,
            "owner_name": who,
            "human_task_id": human.get("id"),
        }

    kind = work_kind(title, tags, detail)
    # This exact task type is created by the reviewed single-email offer. An
    # "Invite ..." sentence in its constraints must not select the older
    # invitation template or lose the composed draft in the response.
    review_email = title in {"Draft one interview email for me to review", "Draft one outreach email for me to review"}
    from app.chat.email_revision import TITLE as EMAIL_REVISION_TITLE
    email_revision = title == EMAIL_REVISION_TITLE
    invitation_draft = not review_email and _is_interview_invitation_draft(title, detail)
    from app.chat.steer import is_review_draft

    review_draft = review_email or email_revision or (is_review_draft(title, detail) and not invitation_draft and not _is_private_email(title, detail))
    can_close = False
    park_reason = "incomplete:search_is_not_done"
    site_proof: dict | None = None
    social_simulated = False
    research_receipt = None
    research_prepared = False

    if kind == "outreach" and not _is_private_email(title, detail) and not review_draft:
        denied = tool_decision(venture_id, "social.post")
        if denied.get("deny"):
            extra = f"\n\n{denied.get('reason')}"
            park_reason = "incomplete:social_denied"
        else:
            social_result, extra = await _run_social_post(venture_id, title, detail)
            can_close = _social_draft_matches(social_result, venture_id, title, detail)
            social_simulated = can_close
            park_reason = "incomplete:social_receipt_unconfirmed"

    if not review_draft and not _local_brief_only(title) and (kind == "site" or "capability:firstlook.landing" in (tags or []) or _is_site_copy(title, detail, tags)):
        kind = "site"
        denied = tool_decision(venture_id, "code")
        if denied.get("deny"):
            extra += f"\n\n{denied.get('reason')}"
            park_reason = "incomplete:code_denied"
        else:
            from app.chat.steer import parse_steer
            from app.firstlook.service import apply_founder_copy

            parsed = parse_steer(f"{title}\n{detail}")
            headline = None
            waitlist_only = False
            comps: list[str] = []
            for op in parsed:
                if op.kind == "site_copy":
                    headline = op.payload.get("headline") or headline
                    waitlist_only = waitlist_only or bool(op.payload.get("waitlist_only"))
                elif op.kind == "remember_competitors":
                    comps = list(op.payload.get("names") or [])
            if not headline:
                hit = re.search(
                    r"(?:headline|hero)\s+to\s+(.+)$",
                    f"{title} {detail}",
                    re.I | re.S,
                )
                if hit:
                    headline = hit.group(1).strip(" .")[:90]
            if headline or waitlist_only or comps:
                site_proof = await apply_founder_copy(
                    venture_id,
                    headline=headline,
                    waitlist_only=waitlist_only,
                    brief=detail or title,
                    competitors=comps or None,
                )
                if site_proof.get("changed"):
                    extra = (
                        f"\n\nServed landing updated (v{site_proof.get('version')}, "
                        f"sha `{str(site_proof.get('sha256') or '')[:12]}`)."
                    )
                    can_close = True
                else:
                    extra = (
                        "\n\nLanding rewrite did not change the served HTML"
                        + (f" ({site_proof.get('error')})." if site_proof.get("error") else ".")
                    )
                    park_reason = "incomplete:site_unchanged"
            else:
                shipped, ship_note = await _ship_first_look(venture_id, title, detail)
                extra += ship_note
                site_proof = {"changed": shipped}
                can_close = shipped
                park_reason = "incomplete:first_look_html_unchanged"

    elif not review_draft and kind == "prototype":
        denied = tool_decision(venture_id, "code")
        if denied.get("deny"):
            extra += f"\n\n{denied.get('reason')}"
            park_reason = "incomplete:code_denied"
        else:
            files_changed, extra = await _run_prototype(
                venture_id, title, detail, repo, task_id
            )
            can_close = files_changed
            park_reason = "incomplete:no_code_change"

    if _is_private_email(title, detail) and not invitation_draft and not review_draft:
        from app.chat.steer import parse_steer, write_email_draft

        to = "founder"
        for op in parse_steer(detail or title):
            if op.kind == "draft_email":
                to = str(op.payload.get("to") or to)
        write_email_draft(venture_id, to=to, brief=detail or title)
        extra += "\n\nEmail draft saved for your review. Draft only, nothing went out."
        can_close = True
        from app.planning.store import complete_matching_draft_tasks

        complete_matching_draft_tasks(venture_id, to)

    notes = detail
    if review_draft:
        try:
            if email_revision:
                from app.chat.email_revision import compose
                notes = await compose(venture_id, detail)
                if not notes:
                    park_reason = "incomplete:saved_interview_revision_unverified"
            else:
                notes = await _review_drafts(venture_id, title, detail)
        except ProviderCallError as exc:
            from app.llm.provider_errors import confirmed_provider_rejection

            code = "review_draft_" + (exc.code if exc.code == "insufficient_credits" or confirmed_provider_rejection(exc) else "provider_outcome_unknown")
            with factory() as session:
                row = session.get(PlannedTask, task_id, with_for_update=True)
                if not row or row.venture_id != venture_id:
                    raise TaskRunError(404, "not_found", "Task not found.") from exc
                if row.status == "running":
                    _park_incomplete(session, row, "needs_you:" + code)
                session.commit()
            pause = ("Buy more in Settings, then ask me to draft again. Nothing was sent."
                     if code == "review_draft_insufficient_credits" else
                     "Your draft request is paused. Nothing was sent. Open work to check your saved request before asking me to try again.")
            _chat(venture_id, task_id, pause, speaker, kind="needs_you",
                  actions=[{"label": "Open work", "href": f"/v/{venture_id}/tasks", "style": "primary"}],
                  once=True)
            _mark(speaker, venture_id, working=None)
            return {"id": task_id, "status": "needs_you", "title": title,
                    "failure_code": code, "pause_message": pause}
        from app.chat.offers import format_drafts
        can_close = bool(notes)
        extra += (("\n\nDraft only. Nothing was sent or posted. Here it is for your review:\n\n" + format_drafts(notes))
                  if can_close else "\n\nYour email draft could not be prepared. Nothing was sent or posted. "
                  "Tell me the question you want to ask, then ask me to draft again.")
        if not can_close and not email_revision:  # a failed revision keeps its own reason, set above
            park_reason = "incomplete:review_draft_request_unverified"
    elif invitation_draft:
        notes = _interview_invitation(venture_id, detail or title)
        can_close = True
    elif kind == "research":
        workspace_id = None
        with factory() as session:
            from app.db.models import Venture

            ven = session.get(Venture, venture_id)
            workspace_id = ven.workspace_id if ven else None
        from app.tasks.progress import ResearchStopped
        try:
            harness_notes = await _harness_research(
                venture_id=venture_id,
                title=title,
                detail=detail,
                speaker=speaker,
                workspace_id=workspace_id,
            )
            if harness_notes is None:
                from app.tasks.usage import context
                if context() and "explore_web" not in _harness_tools(speaker):
                    from app.tasks.progress import record_tool
                    record_tool("explore_web", {}, None, source_count=0, workspace_id=workspace_id, venture_id=venture_id,
                        error=PermissionError("task_tool_not_allowed"))
                    raise PermissionError("task_tool_not_allowed")
                research = await _research_notes(title, detail, workspace_id=workspace_id, venture_id=venture_id)
            else:
                research = harness_notes
        except (ResearchStopped, PermissionError):
            # Keep the typed task blocker and no-replay journal, while still
            # saving the brief and parking the legacy work item below.
            research = ResearchNotes(f"Research needs review before it can continue. Notes from the brief:\n\n{detail}")
        notes = research.text if isinstance(research, ResearchNotes) else str(research or detail)
        # Legacy coverage stays unverified. A linked task can prove its explicit
        # observed-source count; its whole-task rubric only prepares a note here.
        research_receipt = _research_preparation(venture_id, task_id, title, detail, research)
        if research_receipt:
            can_close = True
            research_prepared = research_receipt["mode"] == "rubric_prepared"
            extra += ("\n\nObserved source pages were saved for the agreed task review. The overall review is still pending."
                if research_prepared else "\n\nThe agreed source count was verified from successfully read pages.")
        elif isinstance(research, ResearchNotes) and research.qualified:
            can_close = False
            park_reason = "incomplete:research_coverage_unverified"
            extra += ("\n\nSourced research notes were saved, but the task's acceptance criteria have not been verified. "
                      "Open Work and click this task's title to review the saved notes, then review or revise the plan. "
                      "This task remains open; saving the note did not complete it.")
        else:
            can_close = False
            park_reason = "incomplete:research_sources_unverified"
            extra += ("\n\nYour research could not verify live source pages. I did not mark your task done. "
                      "Review your saved notes in Work before asking me to try again.")
    elif kind == "recruit":
        extra += "\n\nRecruiting is not done by searching the task title."
        park_reason = "incomplete:recruit_needs_real_outreach"
        can_close = False
    elif kind == "other" and not can_close:
        extra += "\n\nThis work is not done by googling the task title."
        park_reason = "incomplete:search_is_not_done"
    if len(detail or "") > 3500:
        extra += (
            "\n\nThis brief was long, so we saved a structured note instead of "
            "one giant agent run. Split follow-ups in chat if a piece is still open."
        )

    from app.planning.store import task_was_cancelled

    cancelled_reason = task_was_cancelled(task_id)
    with factory() as session:
        live = session.get(PlannedTask, task_id)
        live_status = live.status if live and live.venture_id == venture_id else None
    if cancelled_reason or live_status in {"dismissed", "superseded"}:
        why_stop = cancelled_reason or live_status or "cancelled"
        _write_artifact(
            artifact,
            title,
            f"Status: abandoned ({why_stop})\n\nThis run used the old brief and was not marked done.",
            speaker,
            venture_id=venture_id, task_id=task_id,
        )
        _chat(
            venture_id,
            task_id,
            f"Stopped **{title}**, you retargeted or cancelled this work. The old brief was not marked done.",
            speaker,
        )
        _mark(speaker, venture_id, working=None)
        return {
            "id": task_id,
            "status": "cancelled",
            "artifact": str(artifact),
            "title": title,
            "owner_name": who,
        }

    if not can_close:
        why_open = ancestry_markdown(
            venture_id,
            {"title": title, "why_now": why_now or detail},
        )
        body = "Status: not done\n\n"
        if why_open:
            body += f"{why_open}\n\n"
        body += f"{notes}\n{extra}"
        _write_artifact(artifact, title, body, speaker, venture_id=venture_id, task_id=task_id)
        rel = artifact.relative_to(repo.parent.parent)
        from app.chat.recovery import post_failure

        post_failure(venture_id, step="task.run", kind="task_failed", job_id=task_id,
                     key=f"{task_id}:{utcnow().isoformat()}", code=str(park_reason or "").replace(":", "_"),
                     body_md=extra.strip() or "Your request could not be completed. Tell me what you want to try instead.",
                     actions=[{"label": "Open work", "href": f"/v/{venture_id}/tasks", "style": "secondary"}])
        with factory() as session:
            row = session.get(PlannedTask, task_id)
            if not row or row.venture_id != venture_id:
                raise TaskRunError(404, "not_found", "Task not found.")
            _park_incomplete(session, row, park_reason)
            session.commit()
        _mark(speaker, venture_id, working=None)
        return {
            "id": task_id,
            "status": "blocked",
            "artifact": str(artifact),
            "title": title,
            "owner_name": who,
            "completion": {"mode": "unchanged", "artifact": str(rel)},
            **({"failure_code": park_reason.removeprefix("incomplete:")}
               if park_reason in {"incomplete:research_sources_unverified", "incomplete:research_coverage_unverified"} else {}),
        }

    why = ancestry_markdown(
        venture_id,
        {"title": title, "why_now": why_now or detail},
    )
    body = ("Status: prepared for task verification\n\n" if research_prepared else
        "Status: simulated draft prepared (not published)\n\n" if social_simulated else "Status: completed\n\n")
    if invitation_draft:
        # Put the actual reviewable draft ahead of ancestry in the bounded
        # chat view. A long plan brief must not hide the requested writing.
        body += f"{notes}\n\n"
    if why:
        body += f"{why}\n\n"
    if steer_note:
        body += f"## Circuit breaker steer\n{steer_note}\n\n"
    from app.munder.mailbox import drain_for_stop, send as hive_send

    drained = drain_for_stop(venture_id, str(speaker.get("id") or "god"))
    if drained.get("block"):
        body += f"## Hive inbox\n{drained['reason']}\n\n"
    if not invitation_draft:
        body += f"{notes}\n"
    body += extra
    artifact_receipt = _write_artifact(artifact, title, body, speaker, venture_id=venture_id, task_id=task_id,
                    email_revision_detail=detail if email_revision else None, research_receipt=research_receipt)
    rel = artifact.relative_to(repo.parent.parent)
    if research_prepared:
        with factory() as session:
            row = session.get(PlannedTask, task_id)
            if not row or row.venture_id != venture_id:
                raise TaskRunError(404, "not_found", "Task not found.")
            _park_incomplete(session, row, "incomplete:research_judgment_pending")
            session.commit()
        _chat(venture_id, task_id, "The observed source notes are saved. The agreed task review is still pending.",
            speaker, kind="task_prepared", once=True)
        _mark(speaker, venture_id, working=None)
        return {"id": task_id, "status": "prepared", "artifact": str(artifact), "title": title}
    if social_simulated:
        # Verify the exact venture/task-scoped durable draft and close the owned
        # running task in one transaction, before any completion claim.
        from app.db.models import VentureDocument
        from app.db.session import serialize_writes
        with factory() as session:
            serialize_writes(session, key=f"task-note:{task_id}")
            row = session.scalar(select(PlannedTask).where(PlannedTask.id == task_id).with_for_update())
            note = session.scalar(select(VentureDocument).where(
                VentureDocument.id == f"task-note:{task_id}").with_for_update())
            try:
                provenance = json.loads(note.provenance_json or "{}") if note else {}
            except (ValueError, TypeError):
                provenance = {}
            owned = bool(row and row.venture_id == venture_id and row.status == "running"
                         and row.title == title and (row.detail or row.why_now or "") == detail)
            proved = bool(owned and note and note.venture_id == venture_id and note.kind == "task_note"
                          and note.title == title and isinstance(artifact_receipt, str)
                          and note.body_md == artifact_receipt
                          and provenance == {"task_id": task_id, "source": "task.run"})
            if proved:
                _close_done(session, row)
            elif owned:
                _park_incomplete(session, row, "incomplete:social_draft_unconfirmed")
            session.commit()
        if not proved:
            _chat(venture_id, task_id, "The saved social draft could not be confirmed. This run was not marked done.",
                  speaker, kind="task_run_failed")
            _mark(speaker, venture_id, working=None)
            return {"id": task_id, "status": "blocked", "artifact": str(artifact), "title": title,
                    "completion": {"mode": "unchanged", "artifact": str(rel)}}
    if post_completion:
        _chat(
            venture_id,
            task_id,
            (f"Prepared a simulated draft for **{title}**. Review it in Work; nothing was published.{extra}"
             if social_simulated else f"Finished **{title}**. The saved note is in this venture's Work.{extra}"),
            speaker,
            actions=([{"label": "Open website preview", "href": f"/v/{venture_id}/site", "style": "primary"}]
                     if "Served first-look HTML" in extra else [])
            + [{"label": "Open work", "href": f"/v/{venture_id}/tasks", "style": "secondary"}],
            once=True,
        )
    completion_mode = "local"
    if social_simulated:
        completion_mode = "simulated"
    elif "Published live" in extra:
        completion_mode = "live"
    elif "Served landing updated" in extra or "Served first-look HTML" in extra:
        completion_mode = "site"
    elif "**Simulated**" in extra:
        completion_mode = "simulated"

    from app.state import MEM
    from app.munder.ledger import patch_task as hive_patch, sync_planned_runnable

    MEM[f"munder_progress:{venture_id}"] = True
    completion_summary = f"Prepared simulated draft for {title}" if social_simulated else f"Finished {title}"
    hive_patch(
        venture_id,
        task_id,
        {"status": "done", "assignee": who, "result": completion_summary},
    )
    try:
        sync_planned_runnable(venture_id)
    except Exception:  # noqa: BLE001
        pass
    hive_send(
        venture_id,
        {
            "to": "god",
            "act": "done",
            "subject": completion_summary,
            "body": (notes or title)[:800],
        },
        from_id=str(speaker.get("id") or "office"),
    )

    with factory() as session:
        row = session.get(PlannedTask, task_id)
        if not row or row.venture_id != venture_id:
            raise TaskRunError(404, "not_found", "Task not found.")
        if not social_simulated:
            _close_done(session, row)
        session.commit()
        _mark(speaker, venture_id, working=None)
        return {
            "id": row.id,
            "status": "done",
            "artifact": str(artifact),
            "title": title,
            "owner_name": who,
            "completion": {
                "mode": completion_mode,
                "artifact": str(rel),
            },
            **({"draft_text": notes} if review_draft else {}),
        }


def start_planned_task(venture_id: str, task_id: str) -> dict:
    """Validate, check out and queue a task run; return at once (HTTP-safe, well under the 60s ALB limit).

    The checkout CAS makes a double click a no-op (``already_running``); the
    worker (``task.run``) does the slow part via ``run_task_job``.
    """
    from app.core.jobs import enqueue
    from app.db.models import Venture
    from app.paperclip.checkout import claim_task
    from app.planning.store import get_task

    task = get_task(venture_id, task_id)
    local_coding = _is_local_build_task(task)
    _precheck(venture_id, task_id, requested_build=bool(local_coding))
    if local_coding and task.get("status") not in {"done", "running", "dismissed", "superseded"}:
        return _start_local_build_task(venture_id, task)
    claimed = claim_task(venture_id, task_id)
    if claimed is None:
        existing = get_task(venture_id, task_id)
        if not existing:
            raise TaskRunError(404, "not_found", "Task not found.")
        if existing.get("status") in {"done", "running"}:
            return {"id": existing["id"], "status": existing["status"], "title": existing.get("title"),
                    "already_" + existing["status"]: True}
        raise TaskRunError(409, "checkout_conflict", "Could not check out this task.")
    try:
        with db()() as session:
            ven = session.get(Venture, venture_id)
            # One job per checkout: a retried run after a failure gets a fresh key.
            job_id = enqueue(session, "task.run", {"task_id": task_id}, workspace_id=ven.workspace_id,
                    venture_id=venture_id, dedupe_key=f"task.run:{task_id}:{new_id('run')}", max_attempts=1)
            session.commit()
    except Exception:
        _release(venture_id, task_id, "enqueue_failed")
        raise
    return {"id": task_id, "job_id": job_id, "status": "running", "queued": True, "title": claimed.get("title")}


def _release(venture_id: str, task_id: str, reason: str) -> bool:
    """Return a still-running task to the queue (never touches done/dismissed rows)."""
    with db()() as session:
        row = session.get(PlannedTask, task_id)
        if not row or row.venture_id != venture_id or row.status != "running":
            return False
        row.status = "open"
        row.last_error = reason[:200]
        row.updated_at = utcnow()
        session.commit()
        return True


async def run_task_job(payload: dict) -> None:
    """Worker handler for ``task.run``: do the work; on failure reopen the task and tell the founder."""
    from app.core.jobs import FatalError
    from app.core.local_mode import is_local
    from fastapi import HTTPException

    venture_id, task_id = str(payload.get("venture_id") or ""), str(payload.get("task_id") or "")
    if not venture_id or not task_id:
        raise FatalError("task_run_payload_invalid")
    try:
        result = await execute_planned_task(venture_id, task_id, already_claimed=True)
        if is_local() and result.get("status") == "queued" and result.get("device_work_order_id"):
            _chat(venture_id, task_id,
                  "Your request is saved for your coding tool; open Build activity to watch or stop it and review its checked result.",
                  None, actions=[{"label": "Watch your build", "href": result["review_path"], "style": "primary"}], once=True)
        if result.get("status") == "needs_you" and str(result.get("failure_code") or "").startswith("review_draft_"):
            raise TaskRunError(409, result["failure_code"], "The saved draft request needs review before continuing.")
        if result.get("failure_code") in {"research_sources_unverified", "research_coverage_unverified"}:
            # The note-saving handler returned, but the research work did not
            # complete. Keep its parked state and fail this owned queue attempt.
            raise TaskRunError(409, result["failure_code"], "The planned research task was not verified complete.")
    except Exception as exc:  # noqa: BLE001
        code = exc.code if isinstance(exc, TaskRunError) else "handler_failed:" + type(exc).__name__
        message = None
        if is_local():
            if isinstance(exc, TaskRunError):
                message = exc.message
            elif isinstance(exc, HTTPException) and isinstance(exc.detail, dict):
                code = exc.detail.get("code") or code
                message = exc.detail.get("message")
        if code not in {"checkout_conflict", "not_found"} and _release(venture_id, task_id, "run_failed:" + code):
            from app.planning.store import get_task

            title = (get_task(venture_id, task_id) or {}).get("title") or "that task"
            _chat(venture_id, task_id,
                  message or f"I couldn’t finish **{title}** ({code}). It is back in Work. Press Run to try again.",
                  None, kind="task_run_failed", actions=[{"label": "Open work", "href": f"/v/{venture_id}/tasks", "style": "primary"}])
        raise FatalError(code) from exc
