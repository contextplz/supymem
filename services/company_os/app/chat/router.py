"""Intent router, factual path uses zero LLM; actions execute via execute.py."""

from __future__ import annotations

from app.chat.recovery import founder_failure, log_founder_exception

from app.chat.conversations import pending_key

import asyncio
import re
from dataclasses import replace
from decimal import Decimal
from llm_gateway.errors import ProviderCallError

from app.chat.actions import ChatAction, beta_blocked, get_action
from app.chat.blockers import with_explain
from app.chat.confirm import ConfirmationCard
from app.chat.context import ChatContext, assemble
from app.chat.execute import execute_action, local_capability_reply
from app.chat.ground import answer_from_memory
from app.chat.handlers import cancel, change, general, retarget, status
from app.chat.rephrase import INPUT  # noqa: F401, re-exported for callers
from app.chat.intent import (
    _DO_START,
    _ONE_CLICK_PAGE_RE,
    LOOKUP_INTENTS,
    WORK_INTENTS,
    looks_like_instruction,
    looks_like_question,
    classify_intent,  # noqa: F401, re-exported for unit tests
    requested_venture_screen,
    split_speech_acts,
)
from app.chat.refuse import refuse_revenue
from app.chat.store import append_message
from app.core.local_mode import is_local
from app.org.characters import enrich_org, match_mention, stamp_speaker
from app.org.manager import manager_speaker
from app.org.summary import resolve_org
from app.state import MEM, db
from app.ventures.state_brief import invalidate_venture_state_brief
from app.chat.reply_safety import failed_reply


def resolve_action(intent: str) -> ChatAction | None:
    """Resolve only registered actions; legacy informational intents stay local."""
    try:
        return get_action(intent)
    except ValueError:
        return None


_DOLLAR_RE = re.compile(r"\$\d(?:[\d,]*\d)?(?:\.\d+)?")
# shortcut: explicit English saved-work requests only, use semantic retrieval for other phrasing.
_SAVED_WORK_LOOKUP_RE = re.compile(
    r"^\s*(?:(?:please|can you|could you|would you)\s+)?"
    r"(?:show(?:\s+me)?|retrieve|recall|remind\s+me|what\s+(?:was|were))\b"
    r"(?=[\s\S]*\b(?:questions?|scripts?|documents?|checklists?|plans?|outlines?|agendas?|worksheets?|briefs?)\b)"
    r"[\s\S]*\b(?:earlier|previous(?:ly)?|saved|original|already|before|"
    r"(?:you|we)\s+(?:wrote|drafted|prepared|created))\b", re.IGNORECASE,
)
# shortcut: English proposed-price labels; expand when other proposal wording is observed.
_PROPOSED_PRICE_RE = re.compile(
    r"\bproposed\s+(?:(?:(?:pilot|product|subscription)\s+)?price(?:\s+is|\s*:)?|"
    r"(?:paid\s+)?pilot\s*:\**\s*charge)\s*(?:\*+\s*)?(?P<amount>" + _DOLLAR_RE.pattern + r")", re.I,
)
_NUMBER_WORDS = "zero one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen".split()
_TENS_WORDS = "twenty thirty forty fifty sixty seventy eighty ninety".split()
_BELOW_HUNDRED = (r"(?:" + "|".join(_TENS_WORDS) + r")(?:(?:\s+|-)(?:" + "|".join(_NUMBER_WORDS[1:10]) + r"))?|(?:" + "|".join(_NUMBER_WORDS) + r")")
_BELOW_THOUSAND = (r"(?:(?:" + "|".join(_NUMBER_WORDS[1:10]) + r")\s+hundred(?:\s+(?:and\s+)?(?:" + _BELOW_HUNDRED + r"))?|(?:" + _BELOW_HUNDRED + r"))")
_WORD_DOLLAR_RE = re.compile(r"\b(?P<number>" + _BELOW_THOUSAND + r"(?:\s+thousand(?:\s+(?:and\s+)?" + _BELOW_THOUSAND + r")?)?)\s+dollars?\b", re.I)


_NUMERAL_DOLLAR_RE = re.compile(r"(?<![\d.,-])(\d{1,3}(?:,\d{3})+|\d+)\s+dollars?\b", re.I)


def _stated_dollars(known: str) -> dict[Decimal, str]:
    stated = {Decimal(amount.replace(",", "")[1:]): amount for amount in _DOLLAR_RE.findall(known)}
    # "99 dollars" is the founder's own "$99".
    stated.update({Decimal(amount.replace(",", "")): f"${amount}" for amount in _NUMERAL_DOLLAR_RE.findall(known)})
    values = {word: index for index, word in enumerate(_NUMBER_WORDS)}
    values.update({word: index * 10 for index, word in enumerate(_TENS_WORDS, start=2)})
    # shortcut: English cardinal whole-dollar words below one million, use a currency parser when other word forms are needed.
    for match in _WORD_DOLLAR_RE.finditer(known):
        previous = re.search(r"([\w'-]+)\s*$", known[:match.start()])
        if previous and previous[1].casefold() in {*values, "hundred", "thousand", "million", "billion", "trillion", "and", "minus", "negative"}:
            continue
        if re.match(r"\s+(?:and\s+)?(?:\d+|" + _BELOW_HUNDRED + r")\s+cents?\b", known[match.end():], re.I):
            continue
        total, group = 0, 0
        for word in re.split(r"[\s-]+", match["number"].casefold()):
            if word == "hundred":
                group *= 100
            elif word == "thousand":
                total, group = total + group * 1000, 0
            elif word != "and":
                group += values[word]
        stated[Decimal(total + group)] = match[0]
    return stated


_EXPERIMENT_PLAN_RE = re.compile(
    r"^(?:(?:plan|prepare|run|start)\s+(?:me\s+)?(?:(?:my|our|the|next|some)\s+)*experiments?\b|"
    r"(?:what|which)\s+(?:(?:experiments?|tests?)\s+(?:should|can|could)\s+(?:i|we)\s+(?:run|try|do|start)|"
    r"(?:should|can|could)\s+(?:i|we)\s+test\s+next)\b|test\s+(?:my|our|this|the)\s+idea\b)", re.I,
)
_EXPERIMENT_REQUEST_RE = re.compile(
    r"^(?:(?:please|can you|could you|would you|help me|let['’]?s)\s+)*"
    r"(?:(?:run|start|try|test|design|create|prepare)\b[^.!?\n]*\b(?:experiment|test)s?\b|"
    r"(?:test|validate|check|try)\s+(?:whether|if|that)\b)", re.I,
)
_NEXT_MOVES_RE = re.compile(
    r"(?:tell\s+me\s*[, :]\s*)?(?:what(?:['’]s| is)? next|what (?:(?:should|can) i do|to do) (?:next|now))\s*[?.!]*", re.I,
)
_PUBLISHED_CLAIM = re.compile(r"\b(published|sent|deployed to production)\b", re.IGNORECASE)
_NEGATED = re.compile(r"\b(?:nothing|not|never|no|without|none|n't)\b[^.!?]*$|n't\b[^.!?]*$", re.IGNORECASE)
_NEW_RESEARCH_SCOPE = re.compile(
    r"\b(?:compare|investigate|evaluate|verify|current|official|sources?|apis?|polic(?:y|ies)|terms|cites?)\b",
    re.IGNORECASE,
)
_BLOCKER_GUIDANCE_RE = re.compile(
    r"\bwhy\s+(?:can['’]?t|cannot|can\s+not)\s+i\s+proceed\b",
    re.I,
)


def _asks_new_research_scope(text: str) -> bool:
    """Do not answer a specific new investigation with an unrelated old dossier."""
    return len(text.split()) >= 9 and bool(_NEW_RESEARCH_SCOPE.search(text))


def _founder_analytics_summary(brief: dict) -> tuple[str, bool]:
    """Describe only the two saved sources rendered by the founder Analytics page."""
    website = brief.get("website") or {}
    analytics = brief.get("analytics") or {}
    parts = ["Opening Analytics."]
    waitlist_count = website.get("waitlist_count")
    has_waitlist_count = type(waitlist_count) is int and waitlist_count >= 0
    if has_waitlist_count:
        parts.append(
            f"The managed waitlist ledger has {waitlist_count} saved consented "
            f"request{'s' if waitlist_count != 1 else ''}; synthetic public-submission tests are excluded."
        )
        parts.append("Email ownership, active product use, and payments are unverified.")
    else:
        parts.append("The managed waitlist count is unavailable.")

    state = analytics.get("state")
    if state == "no_app":
        parts.append("No app yet. Approve a plan and build first. Browser event counts are unavailable until an app exists.")
    elif state in {"recorded", "empty"}:
        counts = analytics.get("counts") or {}
        labels = {"landing_view": "landing-view", "cta_click": "call-to-action click",
                  "signup": "reported signup event", "first_action": "first product action",
                  "payment": "unverified payment event"}
        observed = [f"{counts[key]} {labels.get(key, key.replace('_', ' '))}"
                    + ("s" if counts[key] != 1 else "")
                    for key in analytics.get("steps", []) if type(counts.get(key)) is int and counts[key] >= 0]
        if state == "empty":
            parts.append("No recorded visitor events yet in the selected app's browser-event dashboard.")
        elif observed:
            parts.append("The selected app's dashboard records " + ", ".join(observed) + ".")
        else:
            parts.append("The selected app's browser-event counts are unavailable.")
        parts.append("Browser event totals are independent observations, not unique or verified users or revenue.")
    else:
        parts.append("The selected app's browser-event counts are unavailable.")
    return " ".join(parts), has_waitlist_count or state in {"no_app", "recorded", "empty"}


def _known_amounts(venture_id: str, text: str, reply: dict | None = None) -> str:
    """Founder amounts plus verified spend and cap from the owned reasoning run."""
    bits = [text]
    if reply and reply.get("kind") in {"deep_answer_pending", "deep_answer"}:
        try:
            from app.tasks import reasoning
            if reasoning.enabled():
                for action in reply.get("actions") or []:
                    payload = action.get("payload") if isinstance(action, dict) else None
                    saved = payload.get("reasoning") if isinstance(payload, dict) else None
                    if isinstance(saved, dict) and isinstance(saved.get("run_id"), str):
                        current = reasoning.progress(saved["run_id"], venture_id)["actions"][0]["payload"]["reasoning"]
                        bits.append(f"${current['spend_micro'] / 1_000_000:.2f} ${current['cap_micro'] / 1_000_000:.2f}")
        except Exception as exc:
            log_founder_exception(exc, venture_id, "chat.scrub.reasoning")
    try:
        from app.chat.store import list_messages

        bits += [str(m.get("body_md") or "") for m in list_messages(venture_id, limit=30)
                 if m.get("role") in {"user", "founder"}]
    except Exception as exc:  # noqa: BLE001 - never fail a reply over a scrub allowlist
        log_founder_exception(exc, venture_id, "chat.scrub.messages")
    try:
        from app.db.models import Venture
        from app.ventures.founder_facts import FOUNDER_SOURCES, read

        with db()() as session:
            venture = session.get(Venture, venture_id)
            if venture:
                bits += [fact["quote"] for fact in read(session, venture).values()
                         if fact.get("source") in FOUNDER_SOURCES and fact.get("value")
                         and fact["value"] in fact.get("quote", "")]
    except Exception as exc:
        log_founder_exception(exc, venture_id, "chat.scrub.founder_facts")
    try:
        from app.experiments import ledger

        bits += [f"{r.get('hypothesis') or ''} {r.get('measure') or ''} {r.get('result_text') or ''}"
                 for r in ledger.list_for_venture(venture_id)]
    except Exception as exc:  # noqa: BLE001
        log_founder_exception(exc, venture_id, "chat.scrub.experiments")
    return " ".join(bits)


_INTERNAL_FIELD_RE = re.compile(r"`?\b[a-z]+(?:_[a-z]+)*\s*=\s*(?:True|False|None)\b`?")


# Product rules the model has invented. There is no archive-on-new-venture or one-active-venture rule in code.
_INVENTED_RULE_RE = re.compile(
    r"\b(?:will\s+be\s+archived|(?:gets?|is|are)\s+archived|only\s+one\s+(?:venture|workspace|company|startup|project)\s+(?:can|may|could)\s+be\s+active|"
    r"become\s+the\s+active\s+(?:venture|workspace)|one\s+active\s+(?:venture|workspace)\s+at\s+a\s+time)\b", re.I)


def strip_invented_rules(body: str) -> str:
    """Drop sentences that state a venture archive/active-limit rule the product does not have."""
    parts = re.split(r"(?<=[.!?])\s+", body)
    kept = [p for p in parts if not _INVENTED_RULE_RE.search(p)]
    if len(kept) == len(parts):
        return body
    return " ".join(kept).strip() + " Starting a new venture never archives or replaces this one."


def scrub_hallucinations(body: str, ctx: ChatContext, known: str = "") -> str:
    """Strip unverified dollar amounts / past-tense publish claims at L1; stated and proposed product prices stay.

    Internal field assignments such as ``paused=True`` never reach a founder at any autonomy level."""
    body = strip_invented_rules(_INTERNAL_FIELD_RE.sub("that setting", body))
    if (ctx.governance.get("autonomy") or "L1") == "L1":
        given = _stated_dollars(known)
        proposed = {match.start("amount") for match in _PROPOSED_PRICE_RE.finditer(body)}
        body = _DOLLAR_RE.sub(lambda match: given.get(Decimal(match[0].replace(",", "")[1:]),
            match[0] if match.start() in proposed else "[amount unavailable]"), body)
        if not ctx.website.get("published"):
            identifiers = [match.span() for match in re.finditer(r"https?://[^\s<>]+|[^\s<>@]+@[^\s<>@]+\.[A-Za-z]{2,}", body)]
            def publication_claim(match):
                if any(start <= match.start() < end for start, end in identifiers):
                    return match.group(0)
                clause = re.split(r"[.!?;\n]|\b(?:but|however)\b", body[:match.start()], flags=re.IGNORECASE)[-1]
                return match.group(0) if _NEGATED.search(clause) else "prepared"

            body = _PUBLISHED_CLAIM.sub(publication_claim, body)
    return body


# Work intents that start or queue something; a paused venture answers these with one clear notice.
_PAUSED_BLOCKED = frozenset(
    {"change", "retarget", "refine_site", "site.rewrite", "waitlist.enable", "site.publish", "social_post",
     "start_product_build", "pagebuilder.build", "plan_generate", "work_start", "source_qa", "ceo_review", "slate_override", "retry_job", "outreach_draft", "email_revision"}
)

# Free-form intents where an imperative "send/post/pay/delete/hire ..." could otherwise become a runnable task
# or a model promise. Intents with their own reviewed card (social_post, site.*, plan chain) are left alone.
_NEVER_QUEUE_INTENTS = frozenset(
    {"change", "general", "agents", "leads", "whats_next", "assistant", "status", "slate_override", "money"}
)

_EXECUTABLE = frozenset(
    {
        "status",
        "why",
        "money",
        "leads",
        "research",
        "research.start",
        "agents",
        "whats_next",
        "openworker_search",
        "agent_reach",
        "pause",
        "resume",
        "office_hours",
        "ceo_review",
        "brain_brief",
        "social_guide",
        "social_status",
        "gtm.diagnosis",
        "credits.balance",
        "connections.status",
        "connections.connect",
        "connections.disconnect",
        "content.drafts",
        "outreach.drafts",
        "site.preview",
        "waitlist.test",
    }
)


_AFFIRM_RE = re.compile(
    r"(?:(?:yes|yeah|yep|yup|sure|ok(?:ay)?|please|absolutely|definitely|sounds good|let'?s go|let'?s do it|"
    r"do it|go ahead|go for it|kick (?:it|that) off|kick off|start (?:it|now)|build it|make it happen)[,.!\s]*)+",
    re.I)
_HEDGED_REPLY = re.compile(r"\s*(?:yes|yeah|yep|ok(?:ay)?|sure|maybe|no|nope|not now|hmm+)"
                          r"(?:\s*,?\s*(?:but\s+)?not\s+(?:now|yet))?\s*[.!?]*\s*", re.I)
_BUILD_WORD = re.compile(r"\b(?:build|building|kick(?:ing)? off)\b", re.I)
_EXPLICIT_WEB_LOOKUP = re.compile(
    r"(?:^|[.!?;\n]\s*)(?:(?:please|hey|yes|ok(?:ay)?|sure)[,\s]+)*"
    r"(?:(?:can|could|would|will)\s+you\s+)?(?:please\s+)?"
    r"(?:look\s+up\s+\S|(?:check|search|look|read|fetch)\b[^?;\n]{0,300}"
    r"(?:\bonline\b|\bweb\b|\bwebsite\b|\bsite\b|https?://))", re.I,
)


def _build_offer_affirmed(venture_id: str, text: str) -> str | None:
    """A bare yes / 'kick it off' answering a build offer: the build request it stands for, else None.

    A bare yes can otherwise fall to the model, which then claims a build started when none did.
    """
    if not _AFFIRM_RE.fullmatch((text or "").strip()):
        return None
    from app.chat.store import list_messages

    last = next((m for m in reversed(list_messages(venture_id, limit=6))
                 if m.get("role") == "assistant" and m.get("kind") != "next_moves"), None)
    body = str((last or {}).get("body_md") or "")
    if not last or any(isinstance(a, dict) and a.get("confirmation") for a in last.get("actions") or []):
        return None  # a card is already waiting; the guard tells the founder to tap it
    if "?" not in body or not _BUILD_WORD.search(body[-500:]):
        return None
    return "Kick off the landing page build" if re.search(r"landing|web\s*site", body, re.I) else "Kick off the build"


async def _continue_offered_step(venture_id: str, text: str, actor_subject: str | None) -> dict | None:
    from app.chat.journey import guidance_kind
    from app.chat.store import list_messages
    from app.db.models import Venture
    from app.db.supymem_models import IdeaSession
    from app.intake.evidence import INPUT_KIND
    from app.ventures.current_idea import current_revision
    from app.ventures.lifecycle import is_paused
    from app.ventures.next_step import _facts

    if guidance_kind(text) != "continue" or is_paused(venture_id):
        return None
    with db()() as session:
        venture = session.get(Venture, venture_id)
        revision = current_revision(venture, session) if venture else None
        idea = session.get(IdeaSession, revision.session_id) if revision else None
        if idea is None or idea.state == "recon" and _facts(venture_id).get("first_question_pending"):
            return None
    messages = list_messages(venture_id, limit=1)
    if not messages or messages[-1].get("role") != "assistant":
        return None
    message = messages[-1]
    actions = message.get("actions") or []
    if not actions or any(not isinstance(action, dict) or action.get("confirmation") is not None for action in actions):
        return None
    if len(actions) > 1:
        append_message(venture_id, role="user", body_md=text)
        return append_message(venture_id, role="assistant", body_md="Which step do you want to take? Choose one below.",
                              actions=actions, citations=[{"question": True}])
    action = actions[0]
    if action.get("disabled_reason") or not isinstance(action.get("action"), str) or action["action"] == "blocker.explain":
        return None
    try:
        registered = get_action(action["action"])
    except ValueError:
        return None
    payload = action.get("payload")
    if payload is None:
        payload = {}
    if not isinstance(payload, dict) or registered.consequential and INPUT_KIND.get() == "voice":
        return None
    append_message(venture_id, role="user", body_md=text)
    reply = await execute_action(venture_id, action["action"], payload=payload, actor_subject=actor_subject,
                                 founder_initiated=action["action"] == "job.retry")
    if reply.get("id"):
        return reply
    return append_message(venture_id, role="assistant", body_md=reply["body_md"], kind=reply["kind"],
                          actions=reply.get("actions"), citations=reply.get("citations"))


def _merge_replies(parts: list[dict]) -> dict:
    from app.chat.customers import SavedOutreachReply
    from app.chat.link_context import SourceReply

    bodies = [str(p.get("body_md") or "").strip() for p in parts if p.get("body_md")]
    actions: list = []
    citations: list = []
    kind = "text"
    for p in parts:
        # Two parts can offer the same card; saved twice, neither copy could ever be confirmed.
        actions.extend(item for item in p.get("actions") or [] if not any(_same_card(item, other) for other in actions))
        citations.extend(p.get("citations") or [])
        if p.get("kind") == "confirmation":
            kind = "confirmation"
        elif p.get("kind") == "next_up" and kind != "confirmation":
            kind = "next_up"
    reply = {
        "kind": kind,
        "body_md": "\n\n".join(bodies) or "Done.",
        "actions": actions,
        "citations": citations,
        **({"parts": [part for reply in parts for part in reply.get("parts", [
            {"type": "text", "text": str(reply.get("body_md") or "").strip()}])]} if any(p.get("parts") for p in parts) else {}),
    }
    if parts and all(isinstance(part, SourceReply) for part in parts):
        return SourceReply(reply)
    if parts and all(isinstance(part, SavedOutreachReply) for part in parts):
        reply["actions"] = reply["actions"][:1]
        return SavedOutreachReply(reply)
    return reply


def _product_build_reply(venture_id: str, brief: dict, actor_subject: str | None = None,
                         hint: str = "", task_id: str | None = None, *, skip_startup_review: bool = False) -> dict:
    """Point a build request at the first saved, reviewable prerequisite."""
    if is_local():
        from app.chat.build_path import local_build_reply
        return local_build_reply(venture_id, brief, plan_task_id=task_id, skip_startup_review=skip_startup_review)
    idea = brief.get("idea") or {}
    plan = brief.get("plan") or {}
    last_build = (brief.get("builds") or {}).get("last") or {}
    personal = idea.get("project_intent") == "personal"
    intro = ""
    state = str(last_build.get("state") or "")
    active = {"queued", "reserving_credits", "provisioning_sandbox", "running",
              "syncing_artifacts", "needs_input", "cancelling"}

    if idea.get("paused"):
        route, label = "now", "Review paused venture"
        detail = "This venture is paused. Resume it before preparing more build work."
    elif state in active:
        route, label = "builds", "Review active build"
        from app.chat.journey import build_state_words

        detail = f"The latest build is {build_state_words(state)}; review that run before requesting another."
    elif state == "succeeded" and last_build.get("review_status") != "accepted":
        route, label = "builds", "Review build evidence"
        detail = "The last build finished, but founder acceptance is not saved. Review its source and QA evidence."
    elif state in {"failed", "cancelled", "rejected"} and not last_build.get("closed", True):
        route, label = "builds", "Review build reconciliation"
        detail = "The last build has not closed and settled yet. Review its receipt before requesting another estimate."
    elif not idea.get("confirmed") and (personal or _PAGE_WORDS.search(hint)):
        route, label = "idea", "Confirm the idea"
        detail = "The current idea is not confirmed. Confirm its scope before research or a build plan."
    elif personal:
        route, label = "idea", "Review local build handoff"
        detail = "Review the confirmed idea and prepare the local Codex build handoff."
    else:
        from app.ventures.build_prerequisites import remaining
        from app.chat.build_path import reply

        steps = remaining(venture_id, brief)
        if skip_startup_review:
            steps = [step for step in steps if step[0] != "startup.review"]
        if steps:
            return reply(venture_id, steps)
        plan_detail = {}
        if plan.get("id"):
            from app.planning.compiler import get_plan

            try:
                plan_detail = get_plan(plan["id"])
            except (KeyError, ValueError):
                # A changed plan is a blocker, never permission to estimate.
                plan_detail = {"stale_dependencies": True}
        if plan_detail.get("stale_dependencies") or plan_detail.get("newer_plan_id"):
            return reply(venture_id, [("plan.generate", "Update the build plan")])
        elif state in {"failed", "cancelled", "rejected"}:
            route, label = "builds", "Review failed build"
            detail = (f"The latest build is {state}. Review its saved result and usage before choosing a new estimate; "
                      "it will not retry automatically.")
        else:
            from app.chat import build_chain
            from app.infra.apps import list_apps

            apps = [app for app in list_apps(venture_id) if app.get("build_mode") == "supymem"]
            name = str(idea.get("name") or "").strip()[:120]
            if not apps and not build_chain.eligible(plan_detail):
                slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:63].strip("-")
                if actor_subject and name and len(slug) >= 3:
                    from app.infra.apps import free_slug

                    slug = free_slug(slug)
                    card = ConfirmationCard.for_action(get_action("build.app.create"),
                        interpretation=f"Create a draft managed app named {name} with URL slug {slug}.",
                        consequences=["This saves the app container for a later, separately reviewed build quote.",
                                      "Nothing is published."],
                        resulting_state={"name": name, "slug": slug})
                    return {"kind": "confirmation", "body_md": (intro +
                        f"The plan is approved, but no app exists. Confirm the draft app {name} ({slug}) here; "
                        "then I can prepare its exact build quote. No build or publication was started."),
                        "actions": [card.as_action()], "citations": []}
            return build_chain.reply(venture_id, intro, plan["id"], plan_detail, apps, name, actor_subject,
                                     hint=hint, task_id=task_id)
    body = intro + detail + " No product build or publication was started by this message."
    if route == "idea" and not idea.get("confirmed") and _PAGE_WORDS.search(hint):
        body = "Your landing page needs one step first: confirm your idea. It takes a minute. Nothing was started."
    return {"kind": "navigation", "body_md": body,
            "actions": [{"label": label, "href": f"/v/{venture_id}/{route}", "style": "primary"}],
            "citations": []}


_PAGE_WORDS = re.compile(r"landing|web\s*site|\bpage\b|\bsite\b", re.I)
_GTM_WORDS = re.compile(r"\bgtm\b|go[ -]?to[ -]?market|customers", re.I)


def _one_click_page_reply(venture_id: str, text: str) -> dict | None:
    """Prepare requested page builds in chat; questions keep the optional build link."""
    if not _PAGE_WORDS.search(text or ""):
        return None
    try:
        from app.ventures.next_step import _facts

        facts = _facts(venture_id)
    except Exception as exc:  # noqa: BLE001 - unreadable state keeps the old reviewed path
        log_founder_exception(exc, venture_id, "build.prepare")
        return None
    if (not facts.get("idea_confirmed") and not facts.get("validation_done")) or facts.get("personal"):
        return None
    actions = [{"label": "Build my landing page", "href": f"/v/{venture_id}/site?build=1", "style": "primary"}]
    body = ("One click builds your landing page from your idea and research; no plan is needed. "
            "You see a private preview before anything is public.")
    if not looks_like_question(text) or _DO_START.match(text.strip()) or classify_intent(text) in {"start_product_build", "pagebuilder.build"}:
        actions = [{"label": "Build your landing page", "action": "pagebuilder.build",
                    "payload": {"prepare": True}, "style": "primary"}]
        body = "Your landing page is ready to build. You will see a private preview before anything is public."
    if facts.get("journey_overrides", {}).get("skipped"):
        from app.ventures.journey_overrides import NOTE
        body = "Build a private landing-page draft from the available idea and website; no plan is needed. " + NOTE
    if facts.get("page_build") == "running":
        actions = [{"label": "Watch progress", "href": f"/v/{venture_id}/site", "style": "primary"}]
        body = "Your landing page is being built now. I will post here when it is ready."
    elif facts.get("page_build") == "ready":
        actions = [{"label": "Open my landing page", "href": f"/v/{venture_id}/site", "style": "primary"}]
        body = "Your landing page is built. Open the private preview below."
    if _GTM_WORDS.search(text or ""):
        actions.append({"label": "Find customers", "href": f"/gtm?venture={venture_id}", "style": "secondary"})
    return {"kind": "navigation", "body_md": body, "actions": actions, "citations": []}


def _planned_landing_card(venture_id: str, text: str, actor_subject: str | None = None) -> dict | None:
    """Confirm the approved plan's pending landing Work item; never mint a new task."""
    if is_local() or not re.search(r"landing|web\s*site", text or "", re.I):
        return None
    from app.planning.store import list_current_tasks

    task = next((t for t in list_current_tasks(venture_id)
                 if str(t.get("source") or "").startswith("approved_plan:")
                 and re.search(r"landing|web\s*site", str(t.get("title") or ""), re.I)), None)
    if not task:
        return None
    if "capability:build.run" in (task.get("tags") or []):
        from app.chat.execute import _managed_work_reply
        plan_task = next((tag.removeprefix("plan_task:") for tag in task.get("tags", [])
                          if isinstance(tag, str) and tag.startswith("plan_task:")), "")
        return _managed_work_reply(venture_id, str(task["title"]), plan_task, actor_subject)
    title = str(task["title"])[:120]
    minutes = task.get("est_minutes")
    card = ConfirmationCard.for_action(get_action("work.start"),
        interpretation=f"Start the planned Work item: {title}.",
        consequences=["Uses the existing approved-plan item; no new task is created.",
                      "Builds a private preview for your review. Nothing is published."]
                     + ([f"Estimated effort: about {minutes} minutes."] if minutes else []),
        resulting_state={"task_id": task["id"]})
    return {"kind": "confirmation", "body_md": (
        f"Your approved plan already has **{title}** in Work. Confirm to start that item. "
        "Nothing has started or been published."), "actions": [card.as_action()], "citations": []}


_PLAN_CHAIN = {"plan_resources_none": "plan.resources_none", "plan_quote": "plan.quote",
               "plan_generate": "plan.generate", "plan_approve": "plan.approve", "work_start": "work.start"}
_START_VERB = re.compile(r"^\s*(?:(?:please|ok|now|can you|could you|let'?s|go ahead and)[,\s]+)*(?:start|run|do|begin|kick off|work on)\b", re.I)


def _short(value: object, n: int = 60) -> str:
    return re.sub(r"[\r\n`*\[\]<>\"]", " ", str(value or "Mac task"))[:n].strip()


def _named_task(venture_id: str, text: str) -> bool:
    """Cheap gate before touching the task table: a start verb naming an approved-plan item."""
    if not _START_VERB.match(text or ""):
        return False
    from app.chat import plan_chain

    return plan_chain.named_task_card(venture_id, text) is not None


def _note_guard(venture_id: str, kind: str) -> None:
    """Count a guard refusal by closed kind; telemetry never changes the founder's reply."""
    try:
        from app.analytics.chat_usage import record_guard

        record_guard(venture_id, kind)
    except Exception as exc:  # noqa: BLE001
        log_founder_exception(exc, venture_id, "chat.guard")


def _same_card(left, right) -> bool:
    return (isinstance(left, dict) and isinstance(right, dict) and isinstance(left.get("confirmation"), dict)
            and isinstance(right.get("confirmation"), dict)
            and left["confirmation"].get("action") == right["confirmation"].get("action")
            and left["confirmation"].get("resulting_state", {}) == right["confirmation"].get("resulting_state", {}))


async def _dispatch_intent(
    venture_id: str, ctx: ChatContext, intent: str, text: str, speaker: dict | None,
    actor_subject: str | None = None, source_message_id: str | None = None,
    *, request_id: str | None = None,
) -> dict:
    from app.chat.agent import resumed_question
    from app.chat.execute import start_requested_work, _action_capability, local_capability_reply
    from app.chat.experiments import scan_request
    from app.chat import hosting
    if is_local() and intent in {"start_product_build", "pagebuilder.build"} and hosting.matches(text):
        intent = "hosting"
    page_advice = (is_local() and intent in {"start_product_build", "pagebuilder.build"}
        and _PAGE_WORDS.search(text)
        and looks_like_question(text) and not _DO_START.match(text.strip())
        and classify_intent(text) not in {"start_product_build", "pagebuilder.build"})
    if page_advice:
        intent = "pagebuilder.build"

    if is_local() and scan_request(text) and (blocked := local_capability_reply("research")):
        return blocked

    if not page_advice and is_local() and intent in {"start_product_build", "mac_prepare", "build.tool.start", "pagebuilder.build"}:
        from app.chat.product_build_request import is_product_build_request, local_build_intent

        if build_intent := local_build_intent(text):
            intent = build_intent
        elif is_product_build_request(text):
            intent = "build.tool.start"
        elif intent == "build.tool.start" or (intent == "start_product_build" and classify_intent(text) != "pagebuilder.build"):
            intent = "assistant"
    if is_local() and intent == "site.rewrite":
        from app.chat.site_actions import explicit_page_rewrite

        if not explicit_page_rewrite(text):
            intent = "assistant"
    if is_local() and intent == "start_product_build" and classify_intent(text) == "pagebuilder.build":
        intent = "pagebuilder.build"
    if is_local():
        from app.core.local_capabilities import hidden_request_message

        if reason := hidden_request_message(text, intent):
            return {"kind": "text", "body_md": reason, "actions": [], "citations": []}
    action_name = {"outreach_draft": "outreach.draft", "email_revision": "outreach.draft",
        "experiment_start": "experiment.run", "experiment_from_review": "experiment.run",
        "content.draft": "outreach.draft"}.get(intent, intent)
    if intent == "research" and looks_like_instruction(text):
        action_name = "research.start"
    if (not page_advice and action_name not in {"site.rewrite", "pagebuilder.build"}
            and (capability_name := _action_capability(action_name, {}))):
        if blocked := await asyncio.to_thread(local_capability_reply, capability_name):
            return blocked

    if resumed := resumed_question(venture_id, text):
        intent, text = "assistant", resumed
    if intent == "jarvis.batch_approve":
        from app.chat import customers
        return await customers.execute(venture_id, "gtm.approve", {"all": True}, actor_subject)

    from app.chat.llm_router import _prohibited_act
    if intent in {"experiment_start", "experiment_from_review"} and _prohibited_act(intent, text):
        return {"kind": "refusal", "body_md": "You asked me not to run experiments, so none were started. Ask me to research your market instead, or review your saved work.",
                "actions": [], "citations": []}

    reply = await _prepare_intent(venture_id, ctx, intent, text, speaker, actor_subject,
                                  source_message_id, request_id=request_id)
    requested = {
        "plan_generate": {"plan.generate"}, "plan_revision": {"plan.generate"},
        "plan_resources_none": {"plan.generate"}, "plan_approve": {"plan.approve"},
        "research.update_and_rerun": {"research.update_and_rerun"},
        "start_product_build": {"work.start", "build.start", "build.app.create", "pagebuilder.build", "idea.confirm"},
        "pagebuilder.build": {"pagebuilder.build", "work.start"},
        "work_start": {"work.start", "build.start", "pagebuilder.build"},
        "outreach_draft": {"outreach.draft"}, "email_revision": {"outreach.draft"},
        "experiment_start": {"experiment.run_review", "experiment.run", "experiment.page.configure", "experiment.channel.configure"},
        "experiment_from_review": {"experiment.run_review", "experiment.run", "experiment.page.configure", "experiment.channel.configure"},
        "experiment_record": {"experiment.record"}, "retry_job": {"job.retry", "research.start"} | ({"plan.generate"} if is_local() else set()),
        "site.rewrite": set() if is_local() else {"site.rewrite"}, "site.publish": {"site.publish"},
        "site.unpublish": {"site.unpublish"}, "site.takedown": {"site.unpublish", "site.takedown"},
        "waitlist.enable": {"waitlist.enable"}, "waitlist.close": {"waitlist.close"},
        "refine_site": {"refine_site"}, "social_post": {"social_post"},
        "manager.rename": {"manager.rename"}, "slate_override": {"slate_override"},
        "pause": {"pause", "work.pause", "work.stop_all"},
        "cancel": {"build.cancel"},
        "approve": {"approve"}, "jarvis.cap_send": {"autopilot.send_cap"},
        "jarvis.build_accept": {"build.accept"},
        "jarvis.credits_request": {"credits.request"},
        "ceo_review": {"ceo_review"},
        "new_venture": {"venture.start_new"},
        "idea.pressure_test.prepare": {"idea.pressure_test"},
    }.get(intent, set())
    if intent == "research" and looks_like_instruction(text):
        requested = {"research.start"}
    if intent in {"work_start", "start_product_build", "pagebuilder.build"} and looks_like_question(text) and (
            (intent == "work_start" and not looks_like_instruction(text))
            or (intent in {"start_product_build", "pagebuilder.build"} and not _DO_START.match(text.strip())
                and classify_intent(text) not in {"start_product_build", "pagebuilder.build"})):
        requested = set()
    if intent == "gtm_review":
        from app.chat.intent import requested_customer_search, icp_question
        from app.chat.idea_bridge import informational_request, request_text
        from app.chat.search_recovery import search_forbidden

        escaped_search = (INPUT.get() or {}).get("questionnaire_escaped") and not informational_request(request_text(text))
        if not search_forbidden((INPUT.get() or {}).get("original", text)) and (
                requested_customer_search(text) or escaped_search) and not icp_question(text):
            requested = {"gtm.find"}
    if not _prohibited_act("experiment_start", text) and (looks_like_instruction(text) or scan_request(text)) and any(isinstance(action, dict)
                                            and (action.get("confirmation") is None or isinstance(action.get("confirmation"), dict))
                                            and (action.get("confirmation") or action).get("action") == "experiment.run"
                                            for action in reply.get("actions") or []):
        requested.add("experiment.run")
    if requested:
        return await start_requested_work(venture_id, reply, actor_subject, actions=requested)
    return reply


async def _prepare_intent(
    venture_id: str, ctx: ChatContext, intent: str, text: str, speaker: dict | None,
    actor_subject: str | None = None,
    source_message_id: str | None = None,
    *, request_id: str | None = None,
) -> dict:
    if is_local():
        from app.chat.actions import beta_blocked
        from app.chat.product_build_request import is_product_build_request, local_build_intent
        if build_intent := local_build_intent(text):
            intent = build_intent
        elif intent in {"start_product_build", "mac_prepare", "build.tool.start", "pagebuilder.build"} and is_product_build_request(text):
            intent = "build.tool.start"
        elif intent == "start_product_build":
            intent = "pagebuilder.build" if classify_intent(text) == "pagebuilder.build" else "assistant"
        elif intent == "build.tool.start":
            intent = "assistant"
        if intent == "god_mode.request":
            intent = "god_on"
        action = {"mac_prepare": "mac.task.prepare", "source_qa": "build.accept"}.get(intent)
        if action:
            blocked = await asyncio.to_thread(beta_blocked, action, venture_id)
            if blocked is not None:
                return blocked

    if intent == "venture.rename":
        from app.chat.intent import VENTURE_RENAME_RE

        named = VENTURE_RENAME_RE.fullmatch(text)
        current_name = (ctx.venture or {}).get("name") or ""
        if named is None:
            return {"kind": "text", "body_md": f"You are chatting with {current_name}. To rename it, say \"rename this venture to <name>\". Your venture name was not changed.",
                    "actions": [], "citations": []}
        return await execute_action(venture_id, intent, payload={"name": named.group("name").strip().strip("\"'")},
                                    actor_subject=actor_subject)
    if intent in {"journey.skip_step", "journey.jump_to", "journey.restore_step", "venture.set_website"}:
        from app.ventures.journey_overrides import parse, plan_after_skip, prepare
        parsed = parse(text)
        if parsed and parsed[0] == intent:
            prepared = prepare(venture_id, intent, parsed[1], actor_subject)
            state = prepared["actions"][0]["confirmation"]["resulting_state"]
            reply = await execute_action(venture_id, intent, payload=state, actor_subject=actor_subject)
            follow_up = plan_after_skip(text)
            if (follow_up and intent in {"journey.skip_step", "journey.jump_to"}
                    and reply.get("action_started") is not False and reply.get("kind") not in {"refusal", "needs_you", "confirmation"}):
                next_reply = await _dispatch_intent(venture_id, assemble(venture_id), "plan_generate", follow_up,
                                                  speaker, actor_subject, request_id=request_id)
                reply = _merge_replies([reply, next_reply])
            if is_local():
                from app.chat import experiments

                if experiments.scan_request(text) and (requested_scan := await asyncio.to_thread(experiments.scan_prepare, venture_id, text)) is not None:
                    from app.chat.execute import start_requested_work
                    running = await start_requested_work(venture_id, requested_scan, actor_subject, actions={"experiment.run"})
                    return _merge_replies([reply, running])
            return reply
        return {"kind": "text", "body_md": "Which step would you like to skip, jump to, or restore? Nothing changed.", "actions": [], "citations": []}
    # Hostile, impossible, negated or empty input never reaches a handler that
    # could queue a runnable task, search the web or offer a publish card.
    if intent == "clarify":
        return {"kind": "text", "body_md": text, "actions": [], "citations": []}
    if intent == "send_off":
        from app.chat.send_control import stop_sending_request

        if stop_sending_request(text):
            return await execute_action(venture_id, "autopilot.send_off", actor_subject=actor_subject)
    from app.chat.guard import guard_reply, is_tenant_refusal
    from app.chat.other_venture import other_venture_reply

    from app.org.manager import manager_name

    # jarvis.* phrases are anchored settings/status commands ("allow sending up to 12 a day" is a cap, not a bulk send).
    guarded = None if intent.startswith("jarvis.") else guard_reply(
        venture_id, text, own_name=(ctx.venture or {}).get("name"), manager=manager_name(venture_id))
    from app.chat.other_venture import supplied_portfolio_context
    if guarded is None and supplied_portfolio_context(text):
        from app.chat import assistant
        return await assistant.answer_question(
            venture_id, text, portfolio_comparison=True, request_id=request_id,
            workspace_id=(ctx.venture or {}).get("workspace_id"),
            supplied_evidence_only=not _allows_saved_current_evidence(text),
            guidance="Compare only the founder's supplied descriptions; use saved current-venture evidence only if explicitly requested. "
                     "Other venture records have not been inspected; do not invent their state or claim work was performed. "
                     "Preserve each concept’s supplied counts and stages separately, including invitations sent versus prepared, "
                     "replies, interviews and product readiness. Do not replace distinct counts with a shared summary. "
                     "Do not invent known contacts, warm introductions, running experiments, relationships or confidentiality promises. "
                     "Use only the supplied public-contact paths; absent paths are unknown, not warm leads. "
                     "Treat recent no-reply invitations as pending, not a reason for immediate follow-up. "
                     + assistant._advice_calendar_context()
                     + "Answer the requested comparison and one next action per concept without offering unrelated execution.")
    if guarded is None or is_tenant_refusal(guarded):
        # A named venture the founder can access gets a status line; anything else stays refused.
        other = other_venture_reply(venture_id, text, actor_subject)
        if other is not None:
            return other
    from app.chat.idea_bridge import written_request
    from app.chat.search_recovery import (
        manual_discovery_reply,
        manual_discovery_request,
    )

    if guarded is None and is_local() and classify_intent(text) == "research.competitors":
        from app.chat.llm_router import _prohibited_act

        if not _prohibited_act("research", text):
            return await execute_action(venture_id, "research.competitors", actor_subject=actor_subject)

    if (guarded is None and intent != "site.rewrite" and not manual_discovery_request(text) and written_request(text)
            and not (is_local() and intent in {"pagebuilder.build", "build.tool.start"})):
        from app.chat.assistant import answer_question

        return await answer_question(venture_id, text, request_id=request_id,
                                     workspace_id=(ctx.venture or {}).get("workspace_id"))
    from app.chat.static_qa_guidance import request_run_id, answer as static_qa_answer
    if (failed_static_run := request_run_id(text)) and (guarded is None or str(guarded.get("body_md") or "").startswith("Understood, I won't")):
        return static_qa_answer(venture_id, (ctx.venture or {}).get("workspace_id"), failed_static_run)
    from app.chat.founder_feedback_guidance import requested as founder_feedback_requested, reply as founder_feedback_reply
    if founder_feedback_requested(text) and (guarded is None or str(guarded.get("body_md") or "").startswith("Understood, I won't")):
        return founder_feedback_reply()
    from app.chat.intent import discovery_progress_question
    if discovery_progress_question(text) and (guarded is None or str(guarded.get("body_md") or "").startswith("Understood, I won't")):
        from app.chat import assistant

        return await assistant.answer_question(venture_id, text, request_id=request_id)
    from app.chat import assistant
    if assistant._asks_about_last_result(text) and (guarded is None or str(guarded.get("body_md") or "").startswith("Understood, I won't")):
        return await assistant.answer_question(venture_id, text, request_id=request_id,
            workspace_id=(ctx.venture or {}).get("workspace_id"))
    if intent not in {"outreach_draft", "email_revision"} and manual_discovery_request(text) and (guarded is None or str(guarded.get("body_md") or "").startswith("Understood, I won't")):
        return manual_discovery_reply(text, venture_id=venture_id)
    if guarded is not None:
        from app.chat.intent import declines_gtm
        if declines_gtm(text) and guarded.get("kind") != "refusal":
            from app.chat.execute import find_customers_reply
            return await find_customers_reply(venture_id, text)
        _note_guard(venture_id, "refusal" if guarded.get("kind") == "refusal" else "deflect")
        return guarded
    from app.chat.browser_prepare import explicit_browser_request, prepare_founder_review
    if intent != "openworker_search" and explicit_browser_request(text):
        if is_local() and (blocked := beta_blocked("mac.task.prepare", venture_id)) is not None:
            return blocked
        from app.tools import ToolContext
        from app.tenancy import current_or_none
        scope = current_or_none()
        tool_context = ToolContext(venture_id=venture_id, workspace_id=scope.workspace_id if scope else None)
        output = await asyncio.to_thread(prepare_founder_review, text, tool_context)
        return {"kind": "confirmation" if tool_context.actions else "text",
            "body_md": output if not output.startswith("error:") else
                "I could not confirm a saved browser review. Check the qualified profile and saved reviews in Settings before another request. Nothing was approved or started.",
            "actions": tool_context.actions, "citations": []}
    if is_local() and intent == "build.tool.start":
        if not is_product_build_request(text):
            return {"kind": "text", "body_md": "Ask me explicitly to build your app or implement your plan when you want it to start. You can review your saved plan first.",
                    "action_started": False, "actions": [], "citations": []}
        return await execute_action(venture_id, "build.tool.start", payload={"text": text},
                                    actor_subject=actor_subject, founder_initiated=True)
    if intent == "mac_prepare":
        from app.chat.product_build_request import is_product_build_request
        if is_product_build_request(text):
            from app.build.device_orders import coding_loop_enabled
            from app.authz.workspace import subject_person_id
            try:
                eligible = bool(actor_subject and (is_local() or coding_loop_enabled(subject_person_id(actor_subject))))
            except Exception as exc:
                return founder_failure(exc, venture_id, "your build request",
                    {"action": "chat.reply", "payload": {"text": text}, "retry": True}, safety="Nothing was launched.")
            if not eligible:
                return {"kind": "navigation", "citations": [],
                    "body_md": "Local product building is not enabled for this account. "
                        "It needs an eligible connected Mac and your own signed-in coding agent. Nothing was prepared or started.",
                    "actions": [{"label": "Open Mac settings", "href": f"/v/{venture_id}/settings", "style": "primary"}]}
            return {"kind": "navigation", "citations": [],
                "body_md": "I can prepare a local product plan from your confirmed idea using your signed-in coding agent. "
                    "You review the plan and build limits before it runs. Deployment has its own approval. Nothing has started.",
                "actions": [{"label": "Prepare product plan", "action": "mac.task.prepare",
                    "style": "primary", "text": text, "payload": {"text": text}}]}
        from app.chat.mac_offers import (NATIVE_UNAVAILABLE_REPLY, direct_mac_offer,
                                         explicit_mac_control_request, native_mac_enabled)

        if explicit_mac_control_request(text):
            if blocked := local_capability_reply("background_work"):
                return blocked
            return direct_mac_offer(text) if is_local() or native_mac_enabled() else dict(NATIVE_UNAVAILABLE_REPLY)
    from app.chat.other_venture import portfolio_context

    if portfolio_context(text, [str((ctx.venture or {}).get("name") or "")]):
        from app.chat import assistant

        return await assistant.answer_question(
            venture_id, text, portfolio_comparison=True, request_id=request_id,
            guidance="This is a read-only portfolio comparison or new-idea request. Use only this venture's "
                          "saved state and the founder's supplied descriptions for other ventures. Other venture "
                          "records have not been inspected; do not invent their state or claim work was performed.")
    from app.chat import hosting, service_guides

    if intent == "hosting":
        return hosting.from_text(venture_id, text)
    if intent == "service_guide":
        return service_guides.from_text(venture_id, text)
    if hosting.matches(text):
        return hosting.from_text(venture_id, text)
    if service_guides.matches(text):
        return service_guides.from_text(venture_id, text)
    if intent == "pause_help":
        card = ConfirmationCard.for_action(get_action("pause"), interpretation="Pause this venture?",
            consequences=["Nothing new will start until you resume.", "Work already running finishes or stops safely. Nothing is deleted."])
        return {"kind": "confirmation", "citations": [],
                "body_md": "You can pause right here: tap below or say “pause this venture”. Nothing new starts until you resume.",
                "actions": [replace(card, confirm_label="Pause this venture").as_action()]}
    if intent == "god_off":
        return await execute_action(venture_id, "autopilot.god_off", actor_subject=actor_subject)  # turning it off never needs a card
    if intent == "god_on":
        if is_local():
            from app.chat.actions import beta_blocked

            if blocked := beta_blocked("autopilot.god_on", venture_id):
                return blocked
            return await execute_action(venture_id, "autopilot.god_on", actor_subject=actor_subject, founder_initiated=True)
        from app.autopilot import god_requests, guard, service

        if not god_requests.beta_gate_open(venture_id):
            return god_requests.explain_reply(venture_id)  # early access: explain and offer the request, never enable
        from app.chat.actions import beta_blocked

        if (blocked := beta_blocked("autopilot.god_on", venture_id)) is not None:  # defers to the same gate
            return blocked
        if guard.get_settings(venture_id)["god_mode"]:
            return {"kind": "text", "body_md": "God mode is already on.", "actions": [], "citations": []}
        card = ConfirmationCard.for_action(
            get_action("autopilot.god_on"), interpretation="Turn on God mode (beta)?",
            consequences=[service.WARNING, "I will approve my own confirmation cards and keep taking the next steps, "
                          "with no permission prompts.", *guard.FLOORS])
        return {"kind": "confirmation", "body_md": f"{service.WARNING} Turn on God mode?",
                "actions": [replace(card, confirm_label="Turn on God mode").as_action()], "citations": []}
    if intent == "pause":
        from app.chat.conversations import current_conversation
        from app.chat.store import CLIENT_REQUEST_ID
        from app.ventures.lifecycle import is_paused

        stop_everything = bool(re.search(r"\bstop everything\b", text, re.I))
        payload = {"stop_everything": True} if stop_everything else {}
        conversation = current_conversation()
        if (is_paused(venture_id) or not (actor_subject or (CLIENT_REQUEST_ID.get() and conversation is not None
                and conversation.visibility == "private" and conversation.venture_id == venture_id))):
            return await execute_action(venture_id, "pause", payload=payload, actor_subject=actor_subject)
        card = ConfirmationCard.for_action(
            get_action("pause"), interpretation="Stop everything?" if stop_everything else "Pause this venture?",
            consequences=["Nothing new will start until you resume.",
                          "Your queued and running tasks are cancelled and God mode turns off. Your saved results are kept."
                          if stop_everything else "Work already running finishes or stops safely. Nothing is deleted."],
            resulting_state=payload)
        if stop_everything:
            return {"kind": "confirmation", "body_md": "Stop everything? Confirm to cancel your queued and running tasks "
                    "and turn God mode off. Your saved results are kept.", "actions": [card.as_action()], "citations": []}
        return {"kind": "confirmation", "body_md": "Pause this venture? Confirm and nothing new will start "
                "until you say resume.", "actions": [card.as_action()], "citations": []}
    from app.chat.intent import looks_like_instruction
    if intent in _PAUSED_BLOCKED or (intent == "research" and looks_like_instruction(text)):
        from app.chat.guard import paused_reply
        from app.ventures.lifecycle import is_paused

        if is_paused(venture_id):
            _note_guard(venture_id, "paused")
            return paused_reply(venture_id)
    if intent in _NEVER_QUEUE_INTENTS:
        from app.chat.guard import outreach_task_reply
        call_goal = False
        if source_message_id is not None and re.match(r"^(?:please\s+)?book\b", text.strip(), re.I):
            from app.tasks import chat as task_chat, goals

            call_goal = (local_capability_reply("background_work") is None if is_local() else goals.enabled()) and task_chat.classify(text) == "change"
        if not call_goal and (refused := outreach_task_reply(venture_id, text)) is not None:
            _note_guard(venture_id, "outreach_refused")
            return refused
    from app.intake.assets import chat_asset_reply
    if (asset_reply := chat_asset_reply(venture_id, text)) is not None:
        return asset_reply
    from app.chat.llm_router import _prohibited_act
    if source_message_id is None and is_local() and not _prohibited_act("research", text):
        from app.chat.intent import competitor_research_request

        if competitor_research_request(text):
            return await execute_action(venture_id, "research.competitors" if intent == "research.competitors"
                else "research.start", actor_subject=actor_subject)
    from app.chat import experiments

    if intent != "experiment_from_review" and experiments.scan_request(text) and (requested_scan := await asyncio.to_thread(experiments.scan_prepare, venture_id, text)) is not None:
        if is_local():
            from app.chat.execute import start_requested_work
            return await start_requested_work(venture_id, requested_scan, actor_subject, actions={"experiment.run"})
        return requested_scan
    if is_local() and intent == "experiment.export_sheet":
        return await execute_action(venture_id, intent, payload={"include_leads": False},
                                    actor_subject=actor_subject, founder_initiated=True)
    if intent in {"idea.pressure_test.prepare", "research.competitors"} or (
            intent == "experiment.beliefs.prepare" and (is_local() or experiments.ledger.autopilot_enabled())):
        return await execute_action(venture_id, intent, actor_subject=actor_subject)
    if intent == "experiment_start" and (is_local() or experiments.ledger.autopilot_enabled()) and experiments.requested_hypothesis(text):
        return await experiments.dispatch(venture_id, intent, text)
    if intent != "experiment_from_review" and (prepared_experiment := await asyncio.to_thread(experiments.named_prepare, venture_id, text)) is not None:
        return prepared_experiment
    if source_message_id is not None:
        from fastapi import HTTPException
        from app.tasks import chat as task_chat
        from app.chat.store import idempotent_message

        try:
            authority = task_chat.reply_authority(venture_id, text, source_message_id=source_message_id)
            existing = idempotent_message(venture_id, "assistant", authority.assistant_key,
                                         output_authority=authority.output_authority)
            if existing is not None:
                authority.update(existing)
                return authority
            handled = await task_chat.handle_message(venture_id, text, source_message_id=source_message_id)
            if handled is not None:
                return handled
        except HTTPException:
            raise
        except Exception as exc:
            log_founder_exception(exc, venture_id, "task.reply")
            # A Task may have saved a funded response before its handler failed.
            # Retain the existing request journal; never dial an ordinary fallback.
            raise HTTPException(409, detail={"error": {"code": "chat_request_outcome_unknown"}}) from None
    if intent.startswith("experiment_"):
        return await experiments.dispatch(venture_id, intent, text)
    if intent == "email_revision":
        from app.chat.email_revision import prepare
        return prepare(venture_id, text)
    if intent == "outreach_draft":
        from app.chat import offers

        return offers.draft_prepare(venture_id, request=text)
    if intent == "retry_job":
        from app.chat import failed_jobs

        return failed_jobs.prepare(venture_id, failed_jobs.kind_in(text), (ctx.venture or {}).get("workspace_id"))
    if intent == "plan_revision" or intent in _PLAN_CHAIN:
        from app.planning.compiler import list_plans

        if not list_plans(venture_id):
            from app.chat.idea_bridge import pending_question

            pending = pending_question(venture_id, text, revise=True)
            if pending is not None:
                return await _answer_idea(venture_id, text, pending, actor_subject, persist=False, correction=True)
            if intent == "plan_revision":
                from app.chat.intent import local_plan_revision_request

                if local_plan_revision_request(text):
                    return {"kind": "text", "body_md": "You don't have a saved plan to update yet. Ask me to generate a plan, then ask me to revise it.",
                            "actions": [], "citations": []}
                return await general.handle(ctx, text, speaker, request_id=request_id)
    if intent == "plan_revision":
        from app.chat import plan_chain

        return plan_chain.revise_prepare(venture_id, text)
    if intent in _PLAN_CHAIN or (intent in {"change", "general"} and _named_task(venture_id, text)):
        from app.chat import plan_chain

        if intent == "work_start":
            return plan_chain.next_task_reply(venture_id, actor_subject)
        if intent in _PLAN_CHAIN:
            return await execute_action(venture_id, _PLAN_CHAIN[intent],
                                        payload={"query": text} if intent == "plan_generate" else {},
                                        actor_subject=actor_subject)
        return plan_chain.named_task_card(venture_id, text, actor_subject)
    if intent == "gtm_review":
        from app.chat.execute import find_customers_reply
        from app.chat.search_recovery import search_forbidden

        return await find_customers_reply(venture_id, text,
            start_requested=not search_forbidden((INPUT.get() or {}).get("original", text)))
    if intent == "latest_error":
        from sqlalchemy import select
        from app.chat.blockers import humanize_error
        from core.redact import redact_string
        from app.db.models import Job
        from app.db.supymem_models import BuildRun
        from app.munder.redact import redact_secrets
        from app.ventures.run_status import FAILED, WORK
        from app.ventures.state_brief import get_venture_state_brief

        from app.chat import failed_jobs

        workspace_id = (ctx.venture or {}).get("workspace_id")
        if (named_kind := failed_jobs.kind_in(text)) and (named := failed_jobs.latest_failed(
                venture_id, workspace_id, named_kind)) is not None:
            return failed_jobs.failure_reply(venture_id, named)  # grounded in that job's real error code
        brief = get_venture_state_brief(venture_id, force=True)
        with db()() as session:
            job = session.scalars(select(Job).where(
                Job.venture_id == venture_id, Job.workspace_id == workspace_id,
                Job.status.in_(FAILED), Job.last_error.is_not(None),
            ).order_by(Job.created_at.desc(), Job.id.desc()).limit(1)).first()
            job_error = str(job.last_error or "") if job else ""
            job_kind = str(job.kind or "") if job else ""
            last_build = (brief.get("builds") or {}).get("last") or {}
            build_error = (last_build.get("failure_reason") or last_build.get("error_code")) if last_build.get("state") in {"failed", "cancelled", "rejected"} else None
            build = session.scalar(select(BuildRun).where(
                BuildRun.id == last_build.get("id"), BuildRun.venture_id == venture_id,
                BuildRun.workspace_id == workspace_id,
            )) if build_error else None
            newest_is_build = bool(build_error and build and (
                not job or build.created_at >= job.created_at))
        if job_error and not newest_is_build and job_kind in failed_jobs.KINDS and (
                latest := failed_jobs.latest_failed(venture_id, workspace_id, job_kind)) is not None and latest["code"]:
            return failed_jobs.failure_reply(venture_id, latest)
        if job_error and not newest_is_build:
            route = WORK.get(job_kind, (None, None, "now"))[2].split("#", 1)[0]
            detail = humanize_error(redact_secrets(redact_string(re.sub(r"[\r\n`*\[\]<>]", " ", job_error)))[:240].strip())
            detail = detail if detail.endswith((".", "!")) else detail + "."
            body = f"{detail} Review the saved result before retrying."
            action = {"label": "Review result", "href": f"/v/{venture_id}/{route}", "style": "primary"}
        elif build_error:
            detail = humanize_error(redact_secrets(redact_string(re.sub(r"[\r\n`*\[\]<>]", " ", str(last_build.get("error_code") or build_error))))[:240].strip()).rstrip(".")
            body = f"{detail}. Review the saved build before trying again; no build was restarted."
            action = {"label": "Review build", "href": f"/v/{venture_id}/builds", "style": "primary"}
        else:
            from app.ventures.run_status import snapshot

            recent = (brief.get("device_work") or {}).get("recent") or []
            latest_review = recent[0] if recent and isinstance(recent[0], dict) else {}
            cancelled_review = (latest_review.get("status") == "cancelled"
                                and not latest_review.get("approved")
                                and not latest_review.get("executed"))
            mac_failed = next((i for i in recent if isinstance(i, dict) and i.get("status") in {"failed", "error"}), None)
            body = "No error is recorded for this venture in saved background jobs or the latest build."
            if mac_failed:
                body = (f"The latest failure was a Mac task, \"{_short(mac_failed.get('title'))}\", which did not finish. "
                        "Nothing more was done on the Mac, and no job or build failed.")
            if cancelled_review:
                body += " The latest Mac task review was cancelled before approval; it did not run and is not a product failure."
            recovery_requested = bool(re.search(r"\bnext\s+(?:recovery\s+)?(?:action|step)\b", text, re.I))
            workflow = snapshot(venture_id, workspace_id) if recovery_requested else {}
            saved_action = workflow.get("action") or {}
            href = saved_action.get("href")
            if (workflow.get("state") == "needs_you" and isinstance(href, str)
                    and href.startswith(f"/v/{venture_id}/")):
                label = str(saved_action.get("label") or "Review saved work")[:80]
                body += f" The saved next review is {label}. No task was started."
                action = {"label": label, "href": href, "style": "primary"}
            else:
                body += " Review current status for the next saved action; no task was started."
                action = {"label": "Review current status", "href": f"/v/{venture_id}/now", "style": "primary"}
        # Consider every recent failure, not just background jobs: a Mac run can fail too.
        failed = next((i for i in (brief.get("device_work") or {}).get("recent") or []
                       if isinstance(i, dict) and i.get("status") in {"failed", "error"}), None)
        if failed and (job_error or build_error):
            body += f" A recent Mac task, \"{_short(failed.get('title'))}\", also did not finish."
        reply = {"kind": "navigation", "body_md": body, "actions": [action], "citations": []}
        return with_explain(reply, "job_failed") if (job_error or build_error or failed) else reply
    if intent == "pagebuilder.build":
        planned = _planned_landing_card(venture_id, text, actor_subject)
        if planned:
            return planned
        if classify_intent(text) == "pagebuilder.build" or looks_like_instruction(text):
            return await execute_action(venture_id, "pagebuilder.build", payload={"prepare": True, "query": text},
                                        actor_subject=actor_subject)
        page = _one_click_page_reply(venture_id, text)
        if page:
            return page
        return {"kind": "navigation", "body_md": "Your landing page is ready to build. You will see a private preview before anything is public.",
            "actions": [{"label": "Build your landing page", "action": "pagebuilder.build", "payload": {"prepare": True}, "style": "primary"}],
            "citations": []}
    if intent == "start_product_build":
        if is_local():
            if blocked := beta_blocked("build.start", venture_id):
                return blocked
        from app.ventures.state_brief import get_venture_state_brief

        page = _one_click_page_reply(venture_id, text)
        if page:
            return page
        planned = _planned_landing_card(venture_id, text, actor_subject)
        if planned:
            return planned
        brief = get_venture_state_brief(venture_id, force=True)
        return _product_build_reply(venture_id, brief, actor_subject, hint=text)
    if intent == "new_venture":
        from app.chat import new_venture

        return new_venture.prepare(venture_id, text, actor_subject)
    if intent.startswith("jarvis."):
        from app.chat import jarvis

        return await jarvis.dispatch(venture_id, intent, text, ctx.venture)
    if intent == "open_build":
        from app.ventures.run_status import snapshot

        workflow = snapshot(venture_id, (ctx.venture or {}).get("workspace_id"))
        action = workflow.get("action") or {}
        href = action.get("href")
        prefix = f"/v/{venture_id}/builds"
        if not isinstance(href, str) or not (href == prefix or href.startswith(prefix + "?")):
            href = prefix
        return {"kind": "navigation", "body_md": "Opening this venture's saved build progress.",
                "actions": [{"label": "Open builds", "href": href, "style": "primary"}],
                "citations": []}
    if intent == "open_screen":
        route = requested_venture_screen(text, (ctx.venture or {}).get("name"))
        if route is not None:
            if route == "site":
                return await execute_action(venture_id, "site.preview", actor_subject=actor_subject)
            from app.chat.intent import COMPANY_SCREEN_PATHS

            href = (f"{COMPANY_SCREEN_PATHS[route]}?venture={venture_id}" if route in COMPANY_SCREEN_PATHS
                    else f"/v/{venture_id}" + (f"/{route}" if route else ""))
            body = "Opening this venture's requested screen."
            served_read = None
            if route == "analytics" and re.search(r"\band\s+(?:tell|show|report)\s+(?:me\s+)?(?:what|which)\b", text, re.I):
                from app.ventures.state_brief import get_venture_state_brief

                body, read_saved_state = _founder_analytics_summary(get_venture_state_brief(venture_id, force=True))
                if read_saved_state:
                    served_read = "open_screen.analytics_read"
            if route == "builds" and re.search(r"\band\s+(?:report|tell|show)\b", text, re.I):
                from app.ventures.state_brief import get_venture_state_brief

                last = (get_venture_state_brief(venture_id, force=True).get("builds") or {}).get("last")
                served_read = "open_screen.builds_read"
                if isinstance(last, dict):
                    state = str(last.get("state") or "unknown")
                    reason = last.get("failure_reason") or last.get("error_code")
                    body = f"Opening Builds. The latest saved build run is {state}."
                    if reason and state in {"failed", "cancelled"}:
                        body += f" Its recorded failure is: {str(reason)[:240]}."
                    body += " This summary does not verify source artifacts, independent QA, or a hosted product. No build was started or modified."
                else:
                    body = "Opening Builds. No saved build run is recorded for this venture. No build was started or modified."
            return {"kind": "navigation", "body_md": body, "_served_read": served_read,
                    "actions": [{"label": "Open screen", "href": href, "style": "primary"}],
                    "citations": []}
        return {"kind": "navigation", "body_md": "This chat controls the current venture only. Switch ventures first.",
                "actions": [], "citations": []}
    if intent == "factual":
        return status.handle_factual(ctx, text)
    if intent == "saved_work":
        from app.planning.saved_work import answer_saved_work

        return {"kind": "text", "body_md": answer_saved_work(venture_id, text),
                "actions": [], "citations": []}
    if intent == "money":
        return refuse_revenue(ctx)
    if intent == "why":
        return await answer_from_memory(venture_id, text)
    if intent == "assistant":
        from app.chat.idea_bridge import request_text

        if _NEXT_MOVES_RE.fullmatch(request_text(text)):
            return await execute_action(venture_id, "whats_next", actor_subject=actor_subject)
        from app.chat.assistant import answer_question

        if _BLOCKER_GUIDANCE_RE.search(text):
            from app.chat.journey import guided_answer
            from app.ventures.run_status import snapshot
            from app.ventures.state_brief import get_venture_state_brief

            brief = get_venture_state_brief(venture_id, force=True)
            workflow = snapshot(
                venture_id,
                (ctx.venture or {}).get("workspace_id"),
                paused=bool((brief.get("idea") or {}).get("paused")),
            )
            return guided_answer(venture_id, brief, workflow)

        return await answer_question(
            venture_id,
            text,
            request_id=request_id,
            workspace_id=(ctx.venture or {}).get("workspace_id"),
        )
    if intent == "source_qa":
        from app.ventures.state_brief import get_venture_state_brief

        brief = get_venture_state_brief(venture_id, force=True)
        device = brief.get("device_work") or {}
        coding = device.get("latest_coding_source")
        if not isinstance(coding, dict):
            recent = device.get("recent") or []
            coding = next((item for item in recent if item.get("coding_provider")
                           and item.get("source_state") == "stored"), None)
        if not coding:
            body = ("I cannot run independent QA yet because this venture has no stored coding-source receipt "
                    "in its recent reviewed Mac tasks. No QA job was started and no product was deployed.")
            return {"kind": "text", "body_md": body, "actions": [], "citations": []}
        href = str(coding.get("review_href") or "")
        source = str(coding.get("source_sha256") or "")
        if not re.fullmatch(r"[a-f0-9]{64}", source):
            source = "recorded on the saved task"
        body = (
            f"The stored source version is {source}, linked to the saved {coding['coding_provider']} Mac task. "
            "I cannot run independent runtime QA on this local-source receipt yet: the current QA runner "
            "accepts managed-builder artifacts, and no source-bound QA run or report exists here. "
            "I have not started QA, rerun the builder, deployed a product, or sent messages. "
            "The next platform step is isolated QA of this exact stored version with a durable report, "
            "followed by your review before hosting."
        )
        actions = ([{"label": "Review stored source", "href": href, "style": "primary"}]
                   if re.fullmatch(r"/v/[A-Za-z0-9_-]+/device-work-orders/[A-Za-z0-9_-]+", href) else [])
        if actions:
            body += " Review this stored source now."
        return {"kind": "navigation" if actions else "text", "body_md": body, "actions": actions, "citations": []}
    if intent == "research.update_and_rerun":
        from app.chat.research_revision import parse, prepare
        if parse(text):
            return prepare(venture_id, text, actor_subject)
        from app.chat.idea_corrections import apply
        if corrected := await asyncio.to_thread(apply, venture_id, text, actor_subject):
            return corrected
        intent = "general"
    if intent == "research":
        # Prefer reading an existing dossier; otherwise ask to start durable research.
        from app.chat.execute import research_needs_start

        if research_needs_start(venture_id):
            if looks_like_instruction(text) and not _asks_new_research_scope(text):
                return await execute_action(venture_id, "research.start", actor_subject=actor_subject)
            from app.chat.build_path import research_card
            MEM[pending_key(venture_id)] = text
            return research_card(venture_id)
        if _asks_new_research_scope(text):
            found = await execute_action(venture_id, "openworker_search", payload={"query": text}, actor_subject=actor_subject)
            if not found.get("citations"):
                return found
            return {**found, "body_md": (
                "I found source leads for this new question. These results are not a saved research dossier "
                "or a verified venture decision.\n\n" + str(found.get("body_md") or "No source result was returned.")
            )}
        return await execute_action(venture_id, "research", payload={"query": text}, actor_subject=actor_subject)
    if intent in _EXECUTABLE:
        if is_local() and intent == "credits.balance":
            from app.chat.jarvis import spend_today

            return spend_today(venture_id)
        result = await execute_action(venture_id, intent, payload={"query": text}, actor_subject=actor_subject)
        # Narrate consequential-looking command results in prose when helpful.
        if intent == "agents" and result.get("body_md"):
            from app.chat.assistant import narrate_action_result

            try:
                return await narrate_action_result(
                    venture_id,
                    question=text,
                    action_result=result,
                    workspace_id=(ctx.venture or {}).get("workspace_id"),
                )
            except Exception as exc:  # noqa: BLE001
                log_founder_exception(exc, venture_id, "gtm.prepare")
                return result
        return result
    if intent == "site.unpublish":
        from app.chat.unpublish import prepare
        return prepare(venture_id)
    if intent == "site.rewrite" and is_local():
        from app.chat.site_actions import explicit_page_rewrite

        if explicit_page_rewrite(text):
            return await execute_action(venture_id, "site.rewrite", payload={"instruction": text.strip()},
                actor_subject=actor_subject, founder_initiated=True)
    if intent in {"site.rewrite", "site.takedown", "waitlist.enable", "waitlist.close", "site.publish"}:
        from app.chat.site_actions import route

        return await asyncio.to_thread(route, venture_id, intent, text)
    if intent == "refine_site":
        MEM[pending_key(venture_id)] = text
        action = get_action("refine_site")
        card = ConfirmationCard.for_action(
            action,
            interpretation="Rebuild the landing page from the locked charter.",
            consequences=["A new site version (refined) will be written and served."],
            resulting_state={"query": text},
        )
        return {
            "kind": "confirmation",
            "body_md": "I can rebuild the site from your charter. Confirm to proceed.",
            "actions": [card.as_action()],
            "citations": [],
        }
    if intent == "approve":
        from app.chat import decisions

        MEM[pending_key(venture_id)] = text
        return decisions.route(venture_id, text)
    if intent == "content.draft":
        from app.chat import posts

        answered = posts.content_reply(venture_id, text)
        if answered:
            return answered
        _, platforms = posts.post_request(text)
        from app.chat.assistant import answer_question

        draft = await answer_question(venture_id, text, guidance=posts.WRITE_GUIDANCE, request_id=request_id, post_draft=True)
        if isinstance(draft, posts.DraftReply):
            checked = scrub_hallucinations(draft["body_md"], ctx, _known_amounts(venture_id, text, draft))
            if checked != draft["body_md"]:
                return failed_reply()
            platform = posts.SLUGS.get(platforms[0], platforms[0]) if platforms else "linkedin"
            await asyncio.to_thread(posts._create_post, ctx.venture["workspace_id"], venture_id, None,
                                    platform, draft["body_md"], None, authored_draft=True)
            invalidate_venture_state_brief(venture_id)
            draft["actions"] = posts.draft_actions(text, draft["body_md"])
        return draft
    if intent == "manager.rename":
        from app.chat.intent import RENAME_RE
        from app.org.manager import _NAME_OK, manager_name

        named = RENAME_RE.match(text)
        if named is None:
            return {"kind": "text", "body_md": "What should I be called? Say \"call yourself <name>\". My name was not changed.",
                    "actions": [], "citations": []}
        name = " ".join((named.group("name") or "").split())
        if not _NAME_OK.match(name):
            return {"kind": "text", "body_md": "That name won't work. Use letters, spaces and . ' - only, up to 40 "
                    "characters. My name was not changed.", "actions": [], "citations": []}
        card = ConfirmationCard.for_action(
            get_action("manager.rename"), interpretation=f"Rename your manager from {manager_name(venture_id)} to {name}.",
            consequences=[f"I will be called {name} in the chat header, in what I say and in voice.",
                          "It applies to every venture in this workspace. You can rename me again any time."],
            resulting_state={"name": name})
        return {"kind": "confirmation", "body_md": f"I can go by {name} from now on. Nothing changes until you confirm.",
                "actions": [card.as_action()], "citations": []}
    if intent == "slate_override":
        MEM[pending_key(venture_id)] = text
        action = get_action("slate_override")
        preview = "Apply the requested task changes."
        try:
            from app.will.override import parse_override
            from app.will.slate import generate_slate, latest_slate

            slate = latest_slate(venture_id) or generate_slate(
                venture_id, because="chat override preview"
            )
            parsed = parse_override(text, slate)
            preview = parsed.interpretation
            if parsed.unclear:
                preview += " " + " ".join(parsed.unclear)
        except Exception as exc:  # noqa: BLE001, S110
            log_founder_exception(exc, venture_id, "task.preview")
        card = ConfirmationCard.for_action(
            action,
            interpretation=preview.replace("slate", "task list"),
            consequences=["The current task list will be replaced by the changes shown here."],
            resulting_state={"query": text},
        )
        return {
            "kind": "confirmation",
            "body_md": preview.replace("slate", "task list"),
            "actions": [card.as_action()],
            "citations": [],
        }
    if intent == "social_post":
        from app.chat.actions import SOCIAL_POST_COMING_SOON, social_post_enabled

        if blocked := local_capability_reply("connected_apps"):
            return blocked
        if not is_local() and not social_post_enabled():
            return dict(SOCIAL_POST_COMING_SOON)
        from app.chat import posts

        content, platforms = posts.post_request(text)
        if not content:
            return {"kind": "text", "body_md": (
                "Tell me the exact text to post, for example: post to LinkedIn: your text. I never post your "
                "instruction itself, so nothing was posted."), "actions": [], "citations": []}
        # The founder gave the exact text and said to post it, so it just happens.
        return await execute_action(venture_id, "social_post", payload={"query": text, "content": content, "platforms": platforms},
                                    actor_subject=actor_subject)
    if intent == "social_connect":
        from app.chat.posts import status_reply

        return status_reply(venture_id)
    if intent == "change":
        result = await change.handle(ctx, text)
        from app.chat.assistant import narrate_action_result

        try:
            narrated = await narrate_action_result(
                venture_id,
                question=text,
                action_result=result,
                workspace_id=(ctx.venture or {}).get("workspace_id"),
            )
            invalidate_venture_state_brief(venture_id)
            return narrated
        except Exception as exc:  # noqa: BLE001
            log_founder_exception(exc, venture_id, "chat.narrate")
            invalidate_venture_state_brief(venture_id)
            return result
    if intent == "retarget":
        invalidate_venture_state_brief(venture_id)
        return retarget.handle(ctx, text)
    if intent == "cancel":
        from app.chat import stop_work

        invalidate_venture_state_brief(venture_id)
        return stop_work.route(venture_id, text) or cancel.handle(ctx, text)
    if intent == "status":
        from app.chat.next_moves import compose
        return await asyncio.to_thread(compose, venture_id)
    return await general.handle(ctx, text, speaker=speaker, request_id=request_id)


def _latest_clarification(venture_id: str) -> dict | None:
    """The still-open clarification question: the latest assistant turn, when it carries answer chips."""
    from app.chat.store import list_messages

    last = next((m for m in reversed(list_messages(venture_id, limit=20))
                 if m.get("role") == "assistant" and m.get("kind") != "next_moves"), None)
    if last and any(isinstance(a, dict) and a.get("action") == "chat.reply"
                    and isinstance(a.get("payload"), dict) and a["payload"].get("question_id")
                    for a in last.get("actions") or []):
        return last
    return None


def _park_question(venture_id: str, text: str, *, persist: bool = True) -> dict:
    """Unusable input is not testimony and does not force the same question again."""
    if persist:
        append_message(venture_id, role="user", body_md=text, kind="text")
    return append_message(venture_id, role="assistant", kind="text", actions=[
        {"label": "See your next action", "action": "whats_next", "style": "primary"},
        {"label": "Back to my question", "action": "idea.review", "style": "secondary"}],
        body_md=stamp_speaker("I didn't catch that. I'll leave this point open and use saved assumptions. "
                             "Choose your next action, or continue clarifying later.",
                              manager_speaker(venture_id)))


def _allows_saved_current_evidence(text: str) -> bool:
    phrase = r"saved\s+current[- ]venture\s+evidence"
    allowed = False
    for match in re.finditer(phrase, text or "", re.I):
        before = re.split(r"[.!?;]", text[:match.start()])[-1]
        after = text[match.end():].split(".", 1)[0]
        if re.search(r"\b(?:do\s+not|don['’]t|never|without|exclude|excluding|ignore|not)\b", before, re.I):
            return False
        if re.match(r"\s+(?:is\s+)?(?:not|never|must\s+(?:not|never)|should\s+(?:not|never))\b", after, re.I):
            return False
        if re.search(r"\b(?:use|using|include|including|with|and)\s+(?:the\s+)?$", before, re.I):
            allowed = True
    return allowed


def _stale_question(venture_id: str, question_id: str, lead: str = "That question was already answered, so I did not save it again.") -> dict:
    """A click on a replaced question saves nothing and leads to whatever is current. Not persisted, so no duplicates."""
    from app.chat.execute import _review_idea

    step = _review_idea(venture_id)
    body = stamp_speaker(lead + "\n\n" + step["body_md"], manager_speaker(venture_id))
    if any(action.get("confirmation") for action in step["actions"]):  # a confirmation is bound to a saved message
        return append_message(venture_id, role="assistant", body_md=body, kind=step["kind"], actions=step["actions"])
    return {"id": f"stale-{question_id}", "role": "assistant", "kind": step["kind"], "actions": step["actions"], "citations": [], "body_md": body}


async def _answer_idea(venture_id: str, text: str, pending: tuple[str, str],
                       actor_subject: str | None = None, *, persist: bool = True, correction: bool | None = None) -> dict:
    from app.idea.service import post_message
    from app.db.supymem_models import ClarifierTurn, IdeaRevision, IdeaSession
    from app.chat.idea_proposal import EDIT_QUESTION, edit
    from sqlalchemy import select

    from app.chat.idea_bridge import _CORRECTION, _plausible_answer
    from app.idea.agent import is_options_request, name_like_reply

    session_id, question_id = pending
    revision_id = None
    revising = bool(correction or (correction is None and _CORRECTION.search(text)))
    if not is_options_request(text):
        from app.chat.conversations import resolve
        from app.chat.store import _partition
        from app.db.models import ChatMessage
        from app.idea.service import loads
        from core.copy import founder_copy
        import hashlib
        import json

        scope = resolve(venture_id)
        turn_nonce = f"idea-turn:{question_id}"
        turn_keys = ["chat:" + hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
                     for identity in ([venture_id, "assistant", turn_nonce], [venture_id, "assistant", turn_nonce, scope.id])]
        with db()() as session:
            question = session.get(ClarifierTurn, question_id)
            shown = question and session.scalar(select(ChatMessage.id).where(
                ChatMessage.venture_id == venture_id, ChatMessage.workspace_id == scope.workspace_id,
                _partition(scope), ChatMessage.role == "assistant",
                (ChatMessage.actions_json.contains(json.dumps({"question_id": question_id})[1:-1], autoescape=True)
                 | ChatMessage.idempotency_key.in_(turn_keys)
                 | ((ChatMessage.kind == "idea_clarification_ready")
                    & ChatMessage.idempotency_key.is_(None)
                    & (ChatMessage.actions_json.is_(None) | ~ChatMessage.actions_json.contains('"question_id"', autoescape=True))
                    & ChatMessage.body_md.contains(founder_copy(question.text), autoescape=True)
                    & (ChatMessage.created_at > question.created_at)))).limit(1))
            revision = session.get(IdeaRevision, question.revision_id) if question and question.revision_id else None
            idea = session.get(IdeaSession, session_id)
            if revision is None and idea and idea.current_revision_id:
                revision = session.get(IdeaRevision, idea.current_revision_id)
            eligible = question and (question.kind == "proposal" and revising
                or shown and revising and not re.search(r"\bback\b", text, re.I)
                or shown and (_plausible_answer(text, question, loads(revision.structured if revision else None))
                    or name_like_reply(text, loads(question.options).get("field"), loads(question.options).get("options") or [])))
        if not eligible:
            return _park_question(venture_id, text, persist=persist)
    if revising:
        # Only a correction can target an awaiting proposal (pending_question); a plain answer needs no lookup.
        with db()() as session:
            turn = session.get(ClarifierTurn, question_id)
            revision_id = turn.revision_id if turn and turn.kind == "proposal" else None
    if revision_id:
        opened = edit(venture_id, {"session_id": session_id, "revision_id": revision_id})
        if opened["kind"] != "idea_clarification_ready":
            return opened
        with db()() as session:
            turn = session.scalar(select(ClarifierTurn).where(ClarifierTurn.session_id == session_id)
                                  .order_by(ClarifierTurn.turn.desc()).limit(1))
            if (turn is None or turn.role != "agent" or turn.kind != "question"
                    or turn.text != EDIT_QUESTION or turn.revision_id != revision_id):
                return opened
            question_id = turn.id
    try:
        accepted = post_message(
            session_id, text, actor_subject=actor_subject,
            expected_question_id=question_id, reply_in_chat=True,
        )
    except RuntimeError as exc:
        if str(exc) != "idea_question_no_longer_pending":
            raise
        # An overlapping submission already advanced the conversation.
        # Return the current chat state without creating a second job.
        return append_message(venture_id, role="assistant", kind="text",
            body_md=stamp_speaker("That question has already moved on. I did not start another clarification run. Review the latest saved question before answering again.", manager_speaker(venture_id)))
    if persist:
        append_message(venture_id, role="user", body_md=text, kind="idea_answer")
    if accepted.get("suggestion_turn_id"):
        from app.idea.runner import _question_reply
        question = accepted["session"]["turns"][-1]
        body, actions = _question_reply(question["text"], question["options"], question["id"])
        body = (accepted.get("note") or question["options"]["note"]) + "\n\n" + body
        kind = "idea_clarification_ready"
    elif accepted.get("gstack_turn_id"):
        from app.gstack.office_hours import question_reply
        body, actions = question_reply(accepted["gstack_turn_id"])
        if accepted.get("note"):
            body = accepted["note"] + "\n\n" + body
        kind = "idea_clarification_ready"
    elif "gstack_turn_id" in accepted:
        kind = "idea_clarification_pending"
        body = ("Thanks. I'm reviewing your idea with your answers. I'll post what is strong, "
                "the biggest risk and one test to try.")
        body += f"\n\n{accepted['note']}" if accepted.get("note") else ""
    else:
        kind = "idea_clarification_pending"
        body = ("Got it, your answer is saved. I'm working out what to ask or propose next "
                "and I'll post it here as soon as it's ready.")
    if not accepted.get("gstack_turn_id") and not accepted.get("suggestion_turn_id"):
        from app.chat.execute import _idea_review_failure

        if failed := _idea_review_failure(venture_id):
            kind, body, actions = failed["kind"], failed["body_md"], failed["actions"]
        else:
            body += " If no summary appears within two minutes, choose Check review."
            actions = [{"label": "Check review", "action": "idea.review", "style": "primary"}]
    saved = (append_message(venture_id, role="assistant", kind=kind,
                           body_md=stamp_speaker(body, manager_speaker(venture_id)), actions=actions)
             if persist else {"kind": kind, "body_md": body, "actions": actions, "citations": []})
    if actor_subject:
        try:
            from app.analytics.chat_usage import record_chat_action
            from app.analytics.events import emit

            workspace_id = (accepted.get("session") or {}).get("workspace_id")
            request_event_id = emit("request.received", person_id=actor_subject,
                workspace_id=workspace_id, venture_id=venture_id,
                props={"source": "chat", "intent_categories": ["idea_answer"],
                       "topics": [], "work_requested": True})
            record_chat_action(venture_id, "idea.answer", "returned")
            if request_event_id:
                emit("request.outcome", person_id=actor_subject,
                    workspace_id=workspace_id, venture_id=venture_id,
                    props={"source": "chat", "request_event_id": request_event_id,
                           "response_kind": "next_up", "offered_actions": 1})
        except Exception as exc:  # noqa: BLE001
            log_founder_exception(exc, venture_id, "chat.answer.usage")
            # Usage telemetry cannot invalidate a saved founder answer.
            pass
    return saved



def _exploration_request(text: str) -> bool:
    from app.chat.idea_bridge import request_text
    from app.experiments import exploration

    command = request_text(text)
    return bool((is_local() or exploration.enabled()) and (
        re.match(r"explor(?:e|ing)\b.*\b(?:options|ideas|segments|markets|approaches)\b", command, re.I)
        or not is_local() and re.match(r"\s*(?:(?:my|our) idea (?:is|:)\s*|i (?:have an idea|want to (?:build|create|start))\b)", text, re.I)))


async def handle_message(venture_id: str, text: str, *, actor_subject: str | None = None,
                         reply_to_question: bool = False, question_id: str | None = None,
                         request_id: str | None = None, job_id: str | None = None) -> dict:
    from app.chat.rephrase import note_outcome
    from app.chat.idea_bridge import pending_question, split_question_reply, written_request

    reply_to_question = reply_to_question or bool(question_id)  # naming the question is a reply to it
    token = INPUT.set({"venture_id": venture_id, "original": text})
    try:
        waiting = None if not reply_to_question and _exploration_request(text) else pending_question(venture_id, "", force=True)
        INPUT.get()["questionnaire_waiting"] = waiting
        INPUT.get()["questionnaire_escaped"] = bool(waiting and split_question_reply(text)[1])
        reply = await _handle_message(venture_id, text, actor_subject=actor_subject,
            reply_to_question=reply_to_question, question_id=question_id, request_id=request_id, job_id=job_id)
        if waiting and INPUT.get().get("questionnaire_escaped") and not written_request(text):
            reply = _continue_clarifying(venture_id, reply)
        return reply
    finally:
        if "interpreted" not in INPUT.get():
            note_outcome("skipped", 0)
        INPUT.reset(token)


def _continue_clarifying(venture_id: str, reply: dict) -> dict:
    import json
    from sqlalchemy import select
    from app.chat.conversations import resolve
    from app.chat.store import _partition, _ser
    from app.db.models import ChatMessage
    from app.db.session import serialize_writes
    from app.chat.next_moves import compose, STATUS_KINDS, _question_action, refused_work

    if refused_work(reply.get("body_md", "")):
        return reply
    if any(isinstance(item, dict) and item.get("question") is True for item in reply.get("citations") or []):
        return reply
    if not any(action.get("action") == "idea.review" for action in compose(venture_id).get("actions") or []):
        return reply

    scope = resolve(venture_id)
    with db()() as session:
        serialize_writes(session, key="chat-messages:" + scope.id)
        message = session.scalar(select(ChatMessage).where(ChatMessage.id == reply.get("id"),
            ChatMessage.venture_id == venture_id, ChatMessage.role == "assistant", _partition(scope)))
        if message is None:
            return reply
        actions = json.loads(message.actions_json or "[]")
        if message.kind in STATUS_KINDS | {"idea_clarification_ready", "confirmation"} or any(
                _question_action(action) or action.get("confirmation") for action in actions):
            return reply
        chip = {"label": "Continue clarifying", "action": "idea.review", "style": "secondary"}
        if not any(action.get("action") == chip["action"] and action.get("label") == chip["label"] for action in actions):
            message.actions_json = json.dumps([*actions, chip])
        session.commit()
        return {**reply, **_ser(message)}


async def _answer_confirmation(venture_id: str, text: str, actor_subject: str | None,
                               question_id: str | None = None) -> dict | None:
    from app.chat.guard import _AFFIRM, pending_confirmation
    from app.chat.idea_bridge import pending_question
    from app.ventures.journey_overrides import ACTIONS
    from app.gstack.office_hours import confirms_summary

    affirmative = _AFFIRM.fullmatch(text.strip()) is not None or confirms_summary(text)
    declined = re.fullmatch(r"\s*(?:no|nope|n|not now|cancel|never\s?mind)\s*[.!?]*\s*", text, re.I) is not None
    named_request = re.match(r"^(?:please\s+|I\s+(?:want|would like)\s+to\s+)", text.strip(), re.I)
    if not affirmative and not declined and not named_request and not _HEDGED_REPLY.fullmatch(text):
        return None
    message = pending_confirmation(venture_id)
    if message is None:
        return None
    if question_id:
        pending = pending_question(venture_id, text, force=True)
        if pending is None or pending[1] != question_id:
            return None
    cards = [action for action in message.get("actions") or []
             if isinstance(action, dict) and isinstance(action.get("confirmation"), dict)]
    if named_request:
        requested_label = text.strip()[named_request.end():].casefold().rstrip(".!?")
        cards = [card for card in cards if str(card.get("label") or "").casefold().rstrip(".!?") == requested_label]
        affirmative = len(cards) == 1
    if len(cards) != 1 or cards[0].get("disabled_reason"):
        return None
    card = cards[0]
    confirmation = card["confirmation"]
    action = confirmation.get("action")
    payload = confirmation.get("resulting_state") or {}
    from app.intake.evidence import INPUT_KIND

    append_message(venture_id, role="user", body_md=text)
    # A bare yes can land on an old card: what leaves the company still needs the founder to name it or use its button.
    if not named_request and (action in {"gtm.send", "outreach.send", "social_post", "approve", "site.publish"} or INPUT_KIND.get() == "voice"):
        return append_message(venture_id, role="assistant", kind="confirmation",
            body_md=f"Tap **{card.get('label') or 'Do it'}** on the card above, or say it by name. Nothing was started.", actions=message.get("actions") or [])
    if affirmative and action not in ACTIONS | {"gtm.route.confirm", "idea.confirm"}:
        from fastapi import HTTPException
        from app.chat.confirmations import start_requested_work

        try:
            return await start_requested_work(venture_id, {**message, "actions": [{**card, "style": "primary"}]}, actor_subject, actions={action})
        except HTTPException:  # the card's saved inputs cannot run from typed text: its button still can
            return append_message(venture_id, role="assistant", kind="confirmation",
                body_md="That needs its saved review first. Use the button below. Nothing was started.", actions=message.get("actions") or [])
    if action == "gtm.route.confirm" and (affirmative or declined):
        from app.db.supymem_models import EntryChatProposal
        from app.entry import chat_start

        with db()() as session:
            proposal = session.get(EntryChatProposal, payload.get("proposal_id"))
            if (proposal is None or proposal.actor_id != actor_subject
                    or proposal.target_venture_id != venture_id or proposal.route != "gtm"):
                raise chat_start.ChatStartError("gtm_proposal_not_found", "That choice is not available in your venture.", 404)
    if declined:
        from app.chat import confirmations, proposals

        confirmations.card(venture_id, message["id"], action, payload, actor_subject=actor_subject)
        if action in ACTIONS:
            proposals.cancel(venture_id, actor_subject, payload["proposal_id"])
        elif action == "gtm.route.confirm":
            from app.entry import chat_start
            chat_start.cancel(payload["proposal_id"], actor_subject)
        confirmations.mark(venture_id, message["id"], action, "cancelled", payload)
        return append_message(venture_id, role="assistant", kind="text",
            body_md="You declined that choice. Choose what you want to do next.",
            actions=[{"label": "See your next step", "action": "whats_next", "style": "primary"}])
    if not affirmative:
        return append_message(venture_id, role="assistant", kind="text",
            body_md="Your choice is still open on the card above. Ask a question or tell me what you want to do.")
    if action == "gtm.route.confirm":
        from app.chat.store import idempotent_message

        await chat_start.confirm(payload["proposal_id"], actor_subject)
        return idempotent_message(venture_id, "assistant", f"entry:{payload['proposal_id']}:assistant")
    reply = await execute_action(venture_id, action, payload=payload, actor_subject=actor_subject)
    return append_message(venture_id, role="assistant", body_md=reply["body_md"],
                          kind=reply["kind"], actions=reply["actions"], citations=reply.get("citations"))


async def _handle_message(venture_id: str, text: str, *, actor_subject: str | None = None,
                          reply_to_question: bool = False, question_id: str | None = None,
                          request_id: str | None = None, job_id: str | None = None) -> dict:
    from app.secrets.detect import (
        classify_paste,
        looks_like_secret,
        withheld_user_copy,
    )
    from app.secrets.service import request_secret

    if looks_like_secret(text):
        if is_local():
            append_message(venture_id, role="user", body_md=withheld_user_copy())
            return append_message(venture_id, role="assistant", kind="needs_you",
                body_md="Your credential was withheld from chat. Open Settings > Keys and connections, save it in the secure field for your provider or use Import .env, then retry your request without the credential.",
                actions=[{"label": "Open Keys and connections", "href": "/settings/local", "style": "primary", "kind": "recovery_link"}], citations=[])
        spec_id = classify_paste(text)
        opened = request_secret(
            spec_id,
            venture_id=venture_id,
            reason="A credential was pasted in chat. Use the form instead.",
            requested_by="chat",
        )
        append_message(venture_id, role="user", body_md=withheld_user_copy())
        label = str((opened.get("form") or {}).get("label") or "that credential")
        from app.chat import navigation_map as nav
        credential_href = f"/v/{venture_id}/connect/github" if spec_id == "github.pat" else nav.path("connections", venture_id)
        guidance = ("Use the GitHub token field on that page." if spec_id == "github.pat" else
                    "Use a supported secure credential field on Connections. If this service has no field there, "
                    "its setup needs support; no credential form was opened from chat.")
        reply = {
            "kind": "needs_you",
            "body_md": f"I withheld {label} from chat. Never paste credentials here. {guidance}",
            "actions": [
                {
                    "label": "Open GitHub secure setup" if spec_id == "github.pat" else "Open Connections",
                    "href": credential_href,
                    "kind": "secret_form",
                    "slot_id": opened.get("slot_id"),
                    "style": "primary",
                }
            ],
            "citations": [],
            "secret_request": {
                "slot_id": opened.get("slot_id"),
                "status": opened.get("status"),
                "env_refs": opened.get("env_refs"),
            },
        }
        saved = append_message(
            venture_id,
            role="assistant",
            body_md=reply["body_md"],
            kind="needs_you",
            actions=reply["actions"],
        )
        return {**reply, "id": saved.get("id")}

    if not question_id and not reply_to_question and text.strip().casefold().startswith("record page counts:"):
        import json
        append_message(venture_id, role="user", body_md=text)
        try:
            counts = json.loads(text.split(":", 1)[1])
            if not isinstance(counts, dict):
                raise ValueError
        except (ValueError, TypeError):
            return append_message(venture_id, role="assistant", body_md="Use Record your page counts on your test readout, fill in the counts and date range, then send that message.", actions=[])
        reply = await execute_action(venture_id, "experiment.measurement.record", payload=counts, actor_subject=actor_subject)
        return append_message(venture_id, role="assistant", body_md=reply["body_md"], kind=reply["kind"], actions=reply["actions"], citations=reply.get("citations"))

    from app.chat.memory_commands import parse as memory_command, dispatch as dispatch_memory
    if memory_command(text) is not None and not reply_to_question and not question_id:
        # Save the complete user instruction in its admitted conversation first.
        # Only that source row may authorize a memory write; no model route or
        # regenerated payload can replace its target or statement.
        source = append_message(venture_id, role="user", body_md=text, kind="memory_command", preserve_exact=True)
        reply = await dispatch_memory(venture_id, source["id"])
        return append_message(venture_id, role="assistant", body_md=reply["body_md"],
                              kind=reply["kind"], citations=reply["citations"], actions=[], preserve_exact=True)

    if not question_id and re.fullmatch(
            r"\s*(?:(?:how\s+(?:do|can)\s+i|can\s+you\s+help\s+me)\s+)?refer\s+(?:a|my)\s+friend[?.!\s]*", text, re.I):
        append_message(venture_id, role="user", body_md=text)
        if is_local():
            return append_message(venture_id, role="assistant", body_md=(
                "Share the link to the installer you used, not your app data folder or keys. "
                "Your workspace stays private. Your friend can set up their own keys in Settings > Keys and connections."
            ), actions=[], citations=[])
        return append_message(venture_id, role="assistant", body_md="Open Refer a friend, then use Copy link or Copy code to share your invitation.",
                              actions=[{"label": "Refer a friend", "action": "referrals.show"}], citations=[])

    from app.chat.navigation_map import purchase_help
    if not question_id and not reply_to_question and (help_reply := purchase_help(venture_id, text)):
        append_message(venture_id, role="user", body_md=text)
        return append_message(venture_id, role="assistant", body_md=help_reply["body_md"], kind=help_reply["kind"],
                              actions=help_reply["actions"], citations=[])

    known_contact = re.fullmatch(r"\s*add\s+someone\s+i\s+know\s*:\s*(.*)", text, re.I | re.S) if not question_id else None
    from app.chat.assets_in_chat import pasted_contacts
    contacts = pasted_contacts(text) if not question_id else None
    if known_contact or contacts:
        append_message(venture_id, role="user", body_md=text)
        payload = {"text": contacts[0], "format": contacts[1]} if contacts else {"text": text}
        reply = await execute_action(venture_id, "contacts.preview", payload=payload, actor_subject=actor_subject)
        return append_message(venture_id, role="assistant", body_md=reply["body_md"], kind=reply["kind"],
                              actions=reply["actions"], citations=reply.get("citations"))

    from app.chat.idea_bridge import direct_request, request_text
    from app.chat.llm_router import _command_evidence

    if not question_id and _SAVED_WORK_LOOKUP_RE.match(request_text(_command_evidence(text))):
        INPUT.get()["questionnaire_escaped"] = False
        append_message(venture_id, role="user", body_md=text)
        reply = await _prepare_intent(venture_id, assemble(venture_id), "assistant", f"Answer only. {text}",
                                      manager_speaker(venture_id), actor_subject, request_id=request_id)
        return append_message(venture_id, role="assistant", body_md=reply["body_md"], kind=reply["kind"],
                              actions=reply["actions"], citations=reply.get("citations"))

    from app.idea.agent import answer_metadata
    from sqlalchemy.exc import SQLAlchemyError
    from app.chat.next_moves import question_asked_last
    from app.db.supymem_models import ClarifierTurn
    from app.idea.service import loads

    waiting = (INPUT.get() or {}).get("questionnaire_waiting")
    question, saved_question, options = None, None, []
    if waiting:
        try:
            with db()() as session:
                question = session.get(ClarifierTurn, waiting[1])
                saved_question = question
                options = loads(question.options if question else None).get("options") or []
                if not (reply_to_question or question_id or question_asked_last(session, venture_id, waiting[1])):
                    question = None
        except SQLAlchemyError:  # options only refine matching; the answer path continues without them
            question, options = None, []

    if not reply_to_question and not question_id and direct_request(text, question=question):
        text = answer_metadata(text)["founder_text"] or text

    import asyncio
    from app.chat import idea_corrections
    from app.chat import customers
    audience_answer = (not reply_to_question and not question_id
                       and customers._answers_audience_question(venture_id, text)
                       and (idea_corrections.parse(text) or {}).keys() == {"customer"})
    # A multi-line labelled reply to a question goes through the answer path, which saves its fields in order.
    # The composer marks every typed message as a reply while a question shows, so a one-line correction still lands here.
    corrected = None if audience_answer or (reply_to_question or question_id) and "\n" in text.strip() else await asyncio.to_thread(idea_corrections.apply, venture_id, text, actor_subject)
    if corrected:
        append_message(venture_id, role="user", body_md=text, kind="idea_answer")
        return append_message(venture_id, role="assistant", body_md=stamp_speaker(corrected["body_md"], manager_speaker(venture_id)),
                              kind=corrected["kind"], actions=corrected["actions"], citations=corrected.get("citations"))

    from app.gstack.office_hours import skips_only_this_question
    question_options = loads(question.options) if question else {}
    if waiting and question and skips_only_this_question(text, field=question_options.get("known_field")
            or question_options.get("gstack_name") or question_options.get("field")):
        if question_id and question_id != waiting[1]:
            return _stale_question(venture_id, question_id)
        INPUT.get()["questionnaire_escaped"] = False
        append_message(venture_id, role="user", body_md=text, kind="idea_answer")
        reply = await _answer_idea(venture_id, "Skip this question", waiting, actor_subject, persist=False)
        return append_message(venture_id, role="assistant", body_md=reply["body_md"], kind=reply["kind"],
                              actions=reply["actions"], citations=reply.get("citations"))

    from app.chat import failed_jobs

    if is_local() and not reply_to_question and not question_id and looks_like_question(text) and re.search(r"\b(?:idea|review|summary)\b", text, re.I):
        from app.chat.execute import _idea_review_failure

        if failed := _idea_review_failure(venture_id):
            append_message(venture_id, role="user", body_md=text)
            return append_message(venture_id, role="assistant", body_md=failed["body_md"], kind=failed["kind"],
                                  actions=failed["actions"], citations=failed["citations"])

    status_request = failed_jobs.STATUS_REQUEST.fullmatch(text) if not reply_to_question and not question_id else None
    if status_request or job_id and failed_jobs.RETRY_REQUEST.match(text):
        append_message(venture_id, role="user", body_md=text)
        action = "jobs.status" if status_request else "job.retry"
        action_payload = {"state": status_request.group(1).lower()} if status_request else {"job_id": job_id}
        reply = await execute_action(venture_id, action, payload=action_payload, actor_subject=actor_subject)
        return append_message(venture_id, role="assistant", body_md=reply["body_md"],
                              kind=reply["kind"], actions=reply["actions"], citations=reply["citations"])

    from app.chat.next_moves import _REPLY_MOVES
    from app.chat.idea_proposal import requested_saved_review
    if not question_id and not reply_to_question and (requested_saved_review(venture_id, text)
            or any(action == "idea.review" and text.strip().casefold().rstrip(".!?") == label.casefold()
                for label, action, payload, pattern in _REPLY_MOVES)):
        append_message(venture_id, role="user", body_md=text)
        reply = await execute_action(venture_id, "idea.review", actor_subject=actor_subject)
        return append_message(venture_id, role="assistant", body_md=reply["body_md"], kind=reply["kind"],
                              actions=reply["actions"], citations=reply.get("citations"))

    from app.chat.idea_proposal import clear_approval
    from app.chat.store import list_messages
    # A card on screen or a question being replied to owns a bare "confirm"; naming the idea or summary always means the idea.
    latest = next((m for m in reversed(list_messages(venture_id, limit=5)) if m.get("role") == "assistant"), None)
    card_showing = any(isinstance(action, dict) and action.get("confirmation") for action in (latest or {}).get("actions") or [])
    bare_owned = (question_id or reply_to_question or card_showing) and text.strip().casefold().rstrip(".!") == "confirm"
    if not bare_owned and clear_approval(text, question_showing=bool((INPUT.get() or {}).get("questionnaire_waiting") or question_id)):
        append_message(venture_id, role="user", body_md=text, kind="idea_confirmation")
        reply = await execute_action(venture_id, "idea.confirm", payload={"typed": True}, actor_subject=actor_subject)
        return append_message(venture_id, role="assistant", body_md=reply["body_md"], kind=reply["kind"],
                              actions=reply["actions"], citations=reply.get("citations"))

    from app.gstack.office_hours import confirms_summary, skips_all_questions
    if confirms_summary(text) and not question_id:
        from app.chat.guard import pending_confirmation
        card = pending_confirmation(venture_id)
        if card:
            reply = await _answer_confirmation(venture_id, text, actor_subject)
            if reply is not None:
                return reply
            return append_message(venture_id, role="assistant", kind="confirmation",
                body_md="Tell me which suggested action you want to run, so I do not choose for you. Nothing was started.", actions=card.get("actions") or [])
    if skips_all_questions(text) or confirms_summary(text) and not question_id:
        import asyncio
        from sqlalchemy import select
        from app.db.models import Venture
        from app.db.supymem_models import IdeaSession, ClarifierTurn
        from app.idea.service import loads, post_message
        from app.chat.execute import _review_idea
        with db()() as session:
            venture = session.get(Venture, venture_id)
            idea = session.scalar(select(IdeaSession).where(IdeaSession.venture_id == venture_id,
                IdeaSession.workspace_id == venture.workspace_id, IdeaSession.state.not_in(("confirmed", "abandoned")))
                .order_by(IdeaSession.created_at.desc()).limit(1)) if venture else None
            latest = session.scalar(select(ClarifierTurn).where(ClarifierTurn.session_id == idea.id)
                .order_by(ClarifierTurn.turn.desc()).limit(1)) if idea else None
            sid, rid = (idea.id, idea.current_revision_id) if idea else (None, None)
            options = loads(latest.options if latest else None)
            proposal = bool(idea and idea.state == "reviewing" and latest and latest.role == "agent" and latest.kind == "proposal")
        if sid and skips_all_questions(text):
            try:
                post_message(sid, text, actor_subject=actor_subject, expected_question_id=question_id, reply_in_chat=True)
            except RuntimeError as exc:
                if str(exc) != "idea_question_no_longer_pending":
                    raise
                return _stale_question(venture_id, question_id)
            append_message(venture_id, role="user", body_md=text, kind="idea_answer")
            reply = await asyncio.to_thread(_review_idea, venture_id)
            from app.chat.intent import _split_clauses
            from app.chat.llm_router import _prohibited_act
            # shortcut: only research resumes after a skip, use batch routing for other follow-up work.
            remaining = " ".join(clause for clause in _split_clauses(text) if not skips_all_questions(clause))
            remaining_intent = classify_intent(remaining)
            if (remaining_intent == "research.start" or remaining_intent == "research" and looks_like_instruction(remaining)) and not _prohibited_act("research", remaining):
                reply = await execute_action(venture_id, "research.prepare", payload={"query": remaining}, actor_subject=actor_subject)
            return append_message(venture_id, role="assistant", body_md=reply["body_md"], kind=reply["kind"], actions=reply["actions"], citations=reply.get("citations"))
        if proposal:
            append_message(venture_id, role="user", body_md=text, kind="idea_confirmation")
            from app.intake.evidence import INPUT_KIND
            reply = await asyncio.to_thread(_review_idea, venture_id)
            if INPUT_KIND.get() == "voice":
                return append_message(venture_id, role="assistant", body_md="Choose the confirmation on screen to continue. Nothing was started.\n\n" + reply["body_md"],
                    kind=reply["kind"], actions=reply["actions"], citations=reply.get("citations"))
            action = next((item.get("action") for item in reply["actions"]
                if item.get("action") in {"idea.confirm", "idea.proposal.approve"}), None)
            if action:
                reply = await execute_action(venture_id, action,
                    payload={"session_id": sid, "revision_id": rid}, actor_subject=actor_subject)
            return append_message(venture_id, role="assistant", body_md=reply["body_md"], kind=reply["kind"], actions=reply["actions"], citations=reply.get("citations"))
        if sid and confirms_summary(text) and options.get("phase") == "gstack" and not (options.get("known_check") or options.get("prefill")):
            append_message(venture_id, role="user", body_md=text, kind="idea_confirmation")
            reply = await asyncio.to_thread(_review_idea, venture_id)
            return append_message(venture_id, role="assistant", kind=reply["kind"], actions=reply["actions"],
                body_md="Your idea summary is saved, but not yet confirmed. You can answer this separate question or skip the questions.\n\n" + reply["body_md"], citations=reply.get("citations"))
        if not sid and skips_all_questions(text):
            if question_id:
                return _stale_question(venture_id, question_id)
            append_message(venture_id, role="user", body_md=text)
            reply = await execute_action(venture_id, "next_moves", actor_subject=actor_subject)
            return append_message(venture_id, role="assistant", kind=reply["kind"], actions=reply["actions"],
                body_md="There are no open questions to skip.\n\n" + reply["body_md"], citations=reply.get("citations"))

    confirmation_reply = await _answer_confirmation(venture_id, text, actor_subject, question_id)
    if confirmation_reply is not None:
        return confirmation_reply

    if not reply_to_question and not question_id:
        continued = await _continue_offered_step(venture_id, text, actor_subject)
        if continued is not None:
            return continued

    from app.chat.idea_bridge import request_text, split_question_reply, written_request

    question_decision = None
    saved_user = None
    if waiting and not (written_request(text) and not reply_to_question and not question_id):
        if question_id and waiting[1] != question_id:
            return _stale_question(venture_id, question_id)
        selected_option = (reply_to_question or not direct_request(text, question=question)) and isinstance(options, list) and text.strip() in options
        if selected_option:
            INPUT.get()["questionnaire_escaped"] = False
            return await _answer_idea(venture_id, text, waiting, actor_subject)
        answer, request = split_question_reply(text,
            options=options if isinstance(options, list) and (reply_to_question or question_id) else None,
            question=question or saved_question)
        INPUT.get()["questionnaire_escaped"] = bool(request)
        # A statement in reply to the live question is its answer; a question back still gets routed.
        from app.chat.idea_bridge import limit_answer
        product_answer = limit_answer(text, question) or (direct_request(text) and not direct_request(text, question=question))
        if not request and (product_answer or (reply_to_question or question_id) and not re.search(r"[?;؟？]\s*$", text)):
            return await _answer_idea(venture_id, text, waiting, actor_subject)
        if not request:
            from app.chat import llm_router
            from app.idea.agent import is_options_request, is_unsure_answer

            # A full sentence while a question waits answers it; a model guess must not lose it. The app sets
            # reply_to_question only for questions with options, so a typed answer often arrives without it.
            # shortcut: word count, question marks and words addressing Charlie; other long requests are saved as the answer.
            statement = (len(text.split()) >= 8 and not text.rstrip().endswith(("?", ";", "？", "؟"))
                         and not re.search(r"\b(?:you|please)\b|\b(?:find|show|give|send|get|tell|help|book|draft|write)\s+(?:me|us)\b|\bfor\s+(?:me|us)\b", text, re.I))
            if not statement and not is_options_request(text) and not is_unsure_answer(text):
                try:
                    question_decision = await llm_router.route(venture_id, text, [("general", text)], assemble(venture_id),
                        request_id=f"{request_id}:router" if request_id else None)
                except Exception as exc:  # noqa: BLE001 - classification is optional; the founder's answer must still save
                    log_founder_exception(exc, venture_id, "chat.intake.route")
                    question_decision = None
                evidence = llm_router._command_evidence(text, preserve_length=True)
                if question_decision and question_decision.clarify:
                    answer, request = "", text
                elif question_decision and any(
                        intent in (WORK_INTENTS | LOOKUP_INTENTS) - {"assistant", "general"}
                        and evidence.strip()
                        and (part not in text or evidence[text.find(part):text.find(part) + len(part)].strip())
                        for intent, part in question_decision.acts):
                    first = text.find(question_decision.acts[0][1])
                    answer = text[:first].strip() if first > 0 else ""
                    request = text[first:] if first >= 0 else text
        if request:
            if answer:
                saved_user = append_message(venture_id, role="user", body_md=text, kind="idea_answer_with_request")
                saved_answer = await _answer_idea(venture_id, answer, waiting, actor_subject, persist=False)
                if saved_answer["kind"] == "text":
                    return saved_answer
                from app.gstack.office_hours import skips_all_questions
                if skips_all_questions(request):
                    from app.chat.execute import _review_idea
                    from app.idea.service import post_message

                    post_message(waiting[0], request, actor_subject=actor_subject, reply_in_chat=True)
                    INPUT.get()["questionnaire_escaped"] = False
                    reply = await asyncio.to_thread(_review_idea, venture_id)
                    return append_message(venture_id, role="assistant", body_md=reply["body_md"], kind=reply["kind"],
                                          actions=reply["actions"], citations=reply.get("citations"))
            INPUT.get()["questionnaire_escaped"] = True
            text = request
            reply_to_question, question_id = False, None

    from app.chat.link_context import site_read_url

    if _EXPLICIT_WEB_LOOKUP.search(text) and (waiting or not site_read_url(text)):
        from app.chat.guard import pending_confirmation

        clarification = pending_confirmation(venture_id)
        if clarification:
            cards = [action["confirmation"] for action in clarification["actions"]
                     if isinstance(action, dict) and isinstance(action.get("confirmation"), dict)]
            if len(cards) == 1 and cards[0].get("interpretation"):
                clarification = {**clarification, "body_md": cards[0]["interpretation"]}
        ctx = assemble(venture_id)
        speaker = manager_speaker(venture_id)
        if saved_user is None:
            append_message(venture_id, role="user", body_md=text)
        reply = await _dispatch_intent(venture_id, ctx, "openworker_search", text, speaker,
                                       actor_subject=actor_subject, request_id=request_id)
        if clarification:
            reply["body_md"] += "\n\n" + re.sub(r"^\s*<!--employee:[^>]*-->\s*", "", clarification["body_md"])
            reply["actions"] = [*(reply.get("actions") or []), *clarification["actions"]]
        return append_message(venture_id, role="assistant", body_md=reply["body_md"], kind=reply["kind"],
                              actions=reply["actions"], citations=reply.get("citations"))

    from app.chat import numbers
    calculation = numbers.command(venture_id, text) if not reply_to_question and not question_id else None
    if calculation is not None:
        from app.analytics.chat_usage import record_served_chat_read

        action, reply = calculation
        if saved_user is None:
            append_message(venture_id, role="user", body_md=text)
        saved = append_message(venture_id, role="assistant", body_md=reply["body_md"],
                               kind=reply["kind"], actions=reply["actions"], citations=reply["citations"])
        record_served_chat_read(venture_id, action)
        return saved

    if not reply_to_question and not question_id and _NEXT_MOVES_RE.fullmatch(request_text(text)):
        if saved_user is None:
            append_message(venture_id, role="user", body_md=text)
        reply = await execute_action(venture_id, "next_moves", actor_subject=actor_subject)
        return append_message(venture_id, role="assistant", body_md=reply["body_md"],
                              kind=reply["kind"], actions=reply["actions"], citations=reply["citations"])

    from app.tasks import routines
    routine = routines.command(text) if not reply_to_question and not question_id else None
    if routine:
        if saved_user is None:
            append_message(venture_id, role="user", body_md=text)
        reply = await execute_action(venture_id, routine[0], payload=routine[1], actor_subject=actor_subject)
        return append_message(venture_id, role="assistant", body_md=reply["body_md"], kind=reply["kind"], actions=reply["actions"], citations=reply.get("citations"))

    from app.chat import reminders
    reminder = reminders.command(text) if not reply_to_question and not question_id else None
    if reminder:
        if saved_user is None:
            append_message(venture_id, role="user", body_md=text)
        reply = await reminders.run(venture_id, *reminder)
        return append_message(venture_id, role="assistant", body_md=reply["body_md"], kind=reply["kind"], actions=reply["actions"], citations=reply.get("citations"))

    # A founder who started in chat should be able to answer the saved first
    # question in the same composer. Only the still-pending, venture-scoped
    # question is eligible; other chat commands keep their normal routing.
    from app.ventures.journey_overrides import chose_customers, parse as journey_command
    command = journey_command(text) if not reply_to_question and not question_id else None
    if command and command[0] == "journey.jump_to" and command[1].get("step") == "find_customers" and chose_customers(venture_id):
        # Already skipped to customers: never a second skip card, go straight to the search.
        if saved_user is None:
            append_message(venture_id, role="user", body_md=text)
        from app.chat.execute import find_customers_reply
        reply = await find_customers_reply(venture_id)
        return append_message(venture_id, role="assistant", body_md=stamp_speaker(reply["body_md"], manager_speaker(venture_id)),
                              kind=reply["kind"], actions=reply["actions"], citations=reply.get("citations"))
    if command:
        if saved_user is None:
            append_message(venture_id, role="user", body_md=text)
        reply = await _dispatch_intent(venture_id, assemble(venture_id), command[0], text,
                                      manager_speaker(venture_id), actor_subject)
        return append_message(venture_id, role="assistant", body_md=reply["body_md"], kind=reply["kind"], actions=reply["actions"], citations=reply.get("citations"))
    from app.chat import new_venture
    answered_idea = new_venture.pending_idea(venture_id, text) if not reply_to_question and not question_id else None
    if answered_idea is not None:
        # One question at a time: the answer to "what is the new venture?" continues that proposal.
        if saved_user is None:
            append_message(venture_id, role="user", body_md=text, kind="new_venture")
        reply = new_venture.prepare(venture_id, text, actor_subject, idea=answered_idea)
        from app.chat.execute import start_requested_work
        reply = await start_requested_work(venture_id, reply, actor_subject, actions={"venture.start_new"})
        return append_message(venture_id, role="assistant", body_md=stamp_speaker(reply["body_md"], manager_speaker(venture_id)),
                              kind=reply["kind"], actions=reply["actions"], citations=reply.get("citations"))
    from app.chat import customers
    from app.chat.search_recovery import search_review_first

    if (is_local() and not reply_to_question and not question_id and search_review_first(text)
            and classify_intent(text) == "gtm_review"):
        if saved_user is None:
            append_message(venture_id, role="user", body_md=text)
        return append_message(venture_id, role="assistant", kind="text",
            body_md="Your next step is to check who you want to reach. Ask me to show your saved audience. "
                    "Then ask me to search public sources when you're ready. No search started, and nothing was sent.")
    customer_command = customers.command(venture_id, text) if not reply_to_question and not question_id else None
    if customer_command:
        # Review, edit, send and replies for the customer journey stay in chat, on the journey's own gates.
        if saved_user is None:
            append_message(venture_id, role="user", body_md=text)
        from app.chat.execute import _action_capability
        capability_name = _action_capability(customer_command[0], customer_command[1])
        reply = (local_capability_reply(capability_name) if capability_name else None) or await customers.run_command(
            venture_id, customer_command[0], customer_command[1], text, actor_subject)
        return append_message(venture_id, role="assistant", body_md=stamp_speaker(reply["body_md"], manager_speaker(venture_id)),
                              kind=reply["kind"], actions=reply["actions"], citations=reply.get("citations"))
    from app.experiments import exploration
    if not reply_to_question and not question_id and _exploration_request(text):
        if saved_user is None:
            append_message(venture_id, role="user", body_md=text)
        reply = local_capability_reply("research") or await exploration.act(venture_id, "experiment.exploration.next", {"idea": text})
        return append_message(venture_id, role="assistant", body_md=reply["body_md"], kind=reply["kind"],
                              citations=reply["citations"], actions=reply["actions"])
    from app.chat.research_revision import parse as research_correction
    correction = research_correction(text) if not reply_to_question and not question_id else None
    from app.chat.idea_bridge import pending_question

    from app.chat.guard import looks_like_noise
    from app.chat.intent import find_customers_request, declines_gtm, strategy_question, discovery_copy_request
    from app.chat.llm_router import _MULTI
    from app.chat.experiments import scan_request

    open_question = None if reply_to_question or not looks_like_noise(text) else _latest_clarification(venture_id)
    customer_request = find_customers_request(text) or declines_gtm(text)
    page_request = is_local() and classify_intent(text) == "pagebuilder.build" and not reply_to_question and not question_id
    copy_request = discovery_copy_request(text) and not reply_to_question and not question_id
    checked_work = not reply_to_question and not question_id and (classify_intent(text) in {
        "idea.pressure_test.prepare", "research.start"} or scan_request(text))
    pending = None if correction or (INPUT.get() or {}).get("questionnaire_escaped") or copy_request or page_request or checked_work or (not reply_to_question and (customer_request or written_request(text))) else pending_question(venture_id, text, force=reply_to_question)
    if open_question is not None:
        return _park_question(venture_id, text, persist=saved_user is None)
    if question_id and (pending is None or pending[1] != question_id):
        # A chip only ever answers the question it was shown with. A repeat click or an old
        # card must not be saved as an answer to a different (newer) question or as chat.
        # Not persisted, so a stale click leaves no duplicate in the conversation.
        return _stale_question(venture_id, question_id)
    if pending is not None:
        return await _answer_idea(venture_id, text, pending, actor_subject, persist=saved_user is None)

    ctx = assemble(venture_id)
    org = enrich_org(resolve_org(venture_id))
    agents = list(org.get("agents") or [])
    speaker = match_mention(text, agents) or manager_speaker(venture_id)
    offered_build = _build_offer_affirmed(venture_id, text)
    llm_routed = False
    acts = split_speech_acts(text)
    task_intent = None
    route_with_model = False
    from app.chat.idea_bridge import _WORK_PREFIX
    from app.experiments.ledger import autopilot_enabled
    experiment_request = re.sub(_WORK_PREFIX, "", request_text(text), flags=re.I)
    experiment_plan = (local_capability_reply("run_for_me_experiments") is None if is_local() else autopilot_enabled()) and _EXPERIMENT_PLAN_RE.match(experiment_request)
    from app.chat.experiments import requested_hypothesis
    from app.chat.browser_prepare import explicit_browser_request
    from app.chat.site_actions import builds_and_publishes, classify_site_intent
    from app.chat.link_context import site_read_url
    from app.chat.product_build_request import is_product_build_request, local_build_intent
    if len(acts) == 1 and acts[0][0] == "new_venture":
        pass
    elif build_intent := local_build_intent(text):
        acts = [(build_intent, text)]
    elif is_local() and is_product_build_request(text):
        acts = [("build.tool.start", text)]
    elif len(acts) == 1 and acts[0][0] == "experiment_from_review":
        pass
    elif (is_local() and len(acts) == 1 and acts[0][0] in {"experiment_record", "experiment_review"}
            and not re.search(r"\b(?:do not|don['’]?t|never)\b[^.!?;\n]*\b(?:log|record|save|store)\b", text, re.I)):
        pass
    elif builds_and_publishes(text) and new_venture.idea_of(text) is None:
        acts = [("site.publish", text)]
    elif classify_intent(text) == "content.draft":
        acts = [("content.draft", text)]
    elif page_request:
        acts = [("pagebuilder.build", text)]
    elif _ONE_CLICK_PAGE_RE.match(text):
        acts = [("start_product_build", text)]
    elif classify_site_intent(text) == "site.rewrite":
        acts = [("site.rewrite", text)]
    elif copy_request or written_request(text):
        acts = [("assistant", text)]
    elif checked_work and not _MULTI.search(text):
        acts = [("experiment_start" if scan_request(text) else classify_intent(text), text)]
    elif len(acts) == 1 and acts[0][0] in {"outreach_draft", "email_revision"}:
        pass
    elif experiment_plan and not (_EXPERIMENT_REQUEST_RE.match(experiment_request) and requested_hypothesis(experiment_request)):
        acts = [("experiment.beliefs.prepare", text)]
    elif _EXPERIMENT_REQUEST_RE.match(experiment_request):
        acts = [("experiment_start", text)]
    elif len(acts) == 1 and acts[0][0] == "venture.rename":
        pass
    elif is_product_build_request(text):
        acts = [("build.tool.start" if is_local() else "mac_prepare", text)]
    elif site_read_url(text):
        acts = [("assistant", text)]  # read the page and answer here, no Mac and no routing-model dispatch
    elif explicit_browser_request(text):
        acts = [("mac_prepare", text)]  # exact qualified browser goal, no routing-model dispatch
    elif offered_build and len(acts) == 1 and acts[0][0] in {"general", "assistant", "status", "change"}:
        acts = [("start_product_build", offered_build)]
    elif customer_request:
        pass
    elif strategy_question(text):
        acts = [("assistant", text)]  # "what are the ways to get my first 10 ..." wants ideas, not the search card
    elif len(acts) == 1 and acts[0][0] in {"send_off", "mac_prepare", "research.update_and_rerun", "connections.connect", "connections.disconnect", "connections.status", "experiment.export_sheet"}:
        pass  # revocation and reviewed Mac preparation must work without the model
    else:
        from app.chat.conversations import actor_id, current_conversation
        from app.chat.store import CLIENT_REQUEST_ID
        from app.tasks.chat import classify as classify_task

        conversation = current_conversation()
        if (CLIENT_REQUEST_ID.get() and actor_subject and conversation is not None
                and conversation.visibility == "private" and conversation.venture_id == venture_id
                and conversation.actor_id == actor_id() == actor_subject):
            task_intent = classify_task(text)
        if task_intent is not None:
            acts = [(task_intent, text)]
        else:
            route_with_model = True
    from app.chat.store import CLIENT_REQUEST_ID
    saved_user = saved_user or append_message(venture_id, role="user", body_md=text,
        kind="new_venture" if any(intent == "new_venture" for intent, _ in acts) else "questionnaire_request" if (INPUT.get() or {}).get("questionnaire_escaped") else "text",
        **({"preserve_exact": True, "idempotency_key": "task-input:" + CLIENT_REQUEST_ID.get()} if task_intent is not None else {}))
    INPUT.get()["message_id"] = saved_user["id"]
    routed_receipts = []
    if route_with_model:
        from app.chat import llm_router
        from app.pagebuilder.jobs import EDIT_RECEIPTS

        receipt_token = EDIT_RECEIPTS.set(routed_receipts)
        try:
            decision = question_decision or await llm_router.route(venture_id, text, acts, ctx,
                                              request_id=f"{request_id}:router" if request_id else None)
        finally:
            EDIT_RECEIPTS.reset(receipt_token)
        llm_routed = decision is not None
        if decision is not None:
            acts = [("clarify", decision.clarify)] if decision.clarify else decision.acts
        elif llm_router.needs_model(text, acts):
            acts = llm_router.negated_fallback(text, acts)  # model unavailable: a prohibition never runs the work
    received_event_id = None
    if actor_subject:
        # Closed intent labels only. Never store chat text, extracted entities,
        # URLs, credentials or the model's answer in operator analytics.
        from app.analytics.events import emit
        from app.chat.analytics_topics import classify_topics
        allowed = LOOKUP_INTENTS | WORK_INTENTS
        categories = sorted({intent.replace(".", "_") if intent in allowed else "other"
                             for intent, _ in acts})
        received_event_id = emit("request.received", person_id=actor_subject,
             workspace_id=(ctx.venture or {}).get("workspace_id"), venture_id=venture_id,
             props={"source": "chat", "intent_categories": categories,
                    "topics": classify_topics(text),
                    "work_requested": any(intent in WORK_INTENTS for intent, _ in acts)})
    replies: list[dict] = []
    for act_index, (intent, clause) in enumerate(acts):
        try:
            replies.append(await _dispatch_intent(venture_id, ctx, intent, clause, speaker,
                                                   actor_subject=actor_subject,
                                                   request_id=f"{request_id}:act:{act_index}" if request_id else None,
                                                   **({"source_message_id": saved_user["id"]} if task_intent is not None else {})))
        except ProviderCallError as exc:
            from llm_gateway.sole import LocalProviderConnectError

            if replies and isinstance(exc, LocalProviderConnectError):
                raise ProviderCallError("provider_outcome_unknown", "Earlier work in this turn may already have run.") from exc
            raise  # A terminal/unknown paid call must not enter another assistant attempt.
        except Exception as exc:  # noqa: BLE001
            log_founder_exception(exc, venture_id, "chat.dispatch")
            if not llm_routed:
                raise
            # A model-chosen label whose handler cannot parse the clause degrades to an honest answer, never a 500.
            _note_guard(venture_id, "llm_route_degraded")
            replies.append(await _dispatch_intent(venture_id, ctx, "assistant", text, speaker,
                                                   actor_subject=actor_subject,
                                                   request_id=f"{request_id}:act:{act_index}:fallback" if request_id else None))
    reply = replies[0] if len(replies) == 1 else _merge_replies(replies)
    edit_job_id = reply.pop("_edit_job_id", None)
    if len(acts) == 1 and acts[0][0] == "site.rewrite" and edit_job_id and routed_receipts:
        from app.pagebuilder.jobs import record_edit_receipts
        record_edit_receipts(edit_job_id, routed_receipts, ctx.venture["workspace_id"], venture_id)
    if len(acts) == 1 and acts[0][0] == "site.rewrite" and reply.pop("_edit_not_delivered", False) and not edit_job_id:
        from app.pagebuilder.jobs import refund_edit_usage
        with db()() as session:
            for usage in routed_receipts:
                refund_edit_usage(usage, ctx.venture["workspace_id"], venture_id, session, saved_user["id"])
            session.commit()
    from app.tasks.chat import TaskReply
    task_reply = len(replies) == 1 and isinstance(reply, TaskReply)
    if task_reply and reply.get("id"):
        return dict(reply)  # Its current source authority was checked before the saved receipt lookup.
    mixed_question_change = len(acts) > 1 and any(
        intent in LOOKUP_INTENTS and looks_like_question(clause) and not looks_like_instruction(clause)
        for intent, clause in acts) and any(intent in WORK_INTENTS for intent, _ in acts)
    from app.chat.static_qa_guidance import VerifiedStaticQaEvidence
    from app.chat.customers import SavedOutreachReply

    exact_qa_evidence = len(replies) == 1 and isinstance(reply, VerifiedStaticQaEvidence)
    from app.chat.posts import DraftReply
    from app.chat.link_context import SourceReply
    quoted_source = isinstance(reply, SourceReply)
    exact_material = quoted_source or exact_qa_evidence or len(replies) == 1 and isinstance(reply, DraftReply)
    saved_outreach = isinstance(reply, SavedOutreachReply)
    if (not quoted_source and not exact_qa_evidence and not task_reply and not saved_outreach and len(acts) == 1 and acts[0][0] in {"general", "assistant", "status"}
            and not any(isinstance(a, dict) and a.get("confirmation") for a in reply.get("actions") or [])):
        from app.chat import claims

        if claims.claims_action(str(reply.get("body_md") or "")):
            _note_guard(venture_id, "claim_replaced")
            reply = claims.honest_reply(venture_id, str(reply.get("body_md") or ""))
        else:
            from app.chat import offers

            reply = claims.with_alternative(offers.with_offers(reply, acts[0][1]))  # a button for every offer and dead end
    if mixed_question_change:
        reply["body_md"] = (
            reply["body_md"].rstrip()
            + "\n\nI only answered the question. Send the change as its own message and I will do it."
        )

    # A saved-work lookup quotes exact stored bytes. Rewriting words such as
    # "No message sent" into "No message prepared" corrupts its evidence.
    # Owned/current static QA contracts and findings likewise quote exact bytes.
    # Only an internal receipt type may bypass rewriting, never a JSON flag.
    from app.chat.intent import _ANSWER_ONLY
    if _ANSWER_ONLY.search(text):
        reply["actions"] = [action for action in reply.get("actions", [])
                            if isinstance(action, dict) and action.get("kind") == "recovery_link"]
    # gtm_review is rendered from saved journey state (real send counts), not model text.
    body = (reply["body_md"] if exact_material or task_reply or saved_outreach or (len(acts) == 1 and acts[0][0] in {"saved_work", "gtm_review"})
            else scrub_hallucinations(reply["body_md"], ctx, _known_amounts(venture_id, text, reply)))
    body = stamp_speaker(body, speaker, preserve_exact=exact_material)
    try:
        saved = append_message(
            venture_id,
            role="assistant",
            body_md=body,
            preserve_exact=exact_material,
            kind=reply.get("kind") or "text",
            citations=reply.get("citations"),
            actions=reply.get("actions"),
            parts=reply.get("parts"),
            **({"idempotency_key": reply.assistant_key, "output_authority": reply.output_authority} if task_reply else {}),
        )
    except Exception as error:
        log_founder_exception(error, venture_id, "chat.reply.save")
        if not task_reply:
            raise
        from fastapi import HTTPException
        if isinstance(error, HTTPException):
            raise
        raise HTTPException(409, detail={"error": {"code": "chat_request_outcome_unknown"}}) from None
    # These direct router reads do not pass execute_action. Count them only
    # after their answer is durably saved; a classified request or offered
    # navigation card alone is not evidence of feature use.
    try:
        from app.analytics.chat_usage import record_served_chat_read

        for (intent, _), part in zip(acts, replies):
            record_served_chat_read(venture_id, intent)
            if part.get("_served_read"):
                record_served_chat_read(venture_id, part["_served_read"])
    except Exception as exc:  # noqa: BLE001
        log_founder_exception(exc, venture_id, "chat.read.usage")
        # Telemetry never turns a saved founder reply into a failed request.
        pass
    if actor_subject and received_event_id:
        from app.analytics.events import emit

        kind = reply.get("kind") or "text"
        response_kind = kind if kind in {"text", "navigation", "confirmation", "needs_you", "next_up"} else "other"
        actions = reply.get("actions")
        emit("request.outcome", person_id=actor_subject,
             workspace_id=(ctx.venture or {}).get("workspace_id"), venture_id=venture_id,
             props={"source": "chat", "request_event_id": received_event_id,
                    "response_kind": response_kind,
                    "offered_actions": min(len(actions), 20) if isinstance(actions, list) else 0})
    return saved
