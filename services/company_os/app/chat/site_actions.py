"""Chat actions for the landing page: preview, rewrite, waitlist, test, publish.

Each executor calls the same service the UI calls. Anything public or paid sits
behind a ConfirmationCard; the executor re-checks its own preconditions, so a
stale or forged confirm cannot skip them. Waitlist rows are "unverified email
submissions" -- never visitors, demand or users.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
from dataclasses import replace
from typing import Any

from app.chat.actions import get_action
from app.chat.confirm import ConfirmationCard

SITE_INTENTS = frozenset({"site.preview", "site.rewrite", "site.takedown", "waitlist.enable", "waitlist.close", "waitlist.test", "site.publish"})
_UNVERIFIED = "Sign-ups show interest, not proof of demand, users or revenue."

_PAGE = r"(?:landing\s*page|landing|web\s*site|website|site|page|homepage)"
_THE = r"(?:(?:the|my|our|this)\s+)?"
# A negated clause ("don't publish yet", "hold off on publishing") is a
# constraint, never a request; drop it before looking for a verb.
_NEG = re.compile(
    r"\b(?:(?:please\s+)?(?:don'?t|do\s+not|never)|not\s+yet|hold\s+off(?:\s+on)?|wait\s+(?:to|before|with)|"
    r"without)\b[^.;!?]*", re.I)
_MENTIONS = re.compile(r"\b(?:publish\w*|go\s+live|waitlist|sign-?ups?|landing|headline|subhead\w*|cta|call\s+to\s+action)\b", re.I)
_PUBLISH_WORD = re.compile(r"\b(?:publish\w*|go\s+live)\b", re.I)
_PUBLISH = re.compile(
    rf"\bpublish\s+(?:it|this|that|now)\b|\bpublish\s+{_THE}(?:\w+\s+){{0,2}}{_PAGE}\b|\bgo\s+live\b|"
    rf"\bput\s+{_THE}{_PAGE}\s+live\b|^\s*(?:please\s+)?publish\s*[.!]?\s*$", re.I)
_BUILD_AND_PUBLISH = re.compile(rf"\b(?:build|create|make|launch|set\s*up)\b[^.?!]*\bpublish\b[^.?!]*\b(?:{_PAGE})\b|"
    rf"\b(?:build|create|make|launch|set\s*up)\b[^.?!]*\b(?:{_PAGE})\b[^.?!]*\b(?:publish|go\s+live|put\s+it\s+live)\b", re.I)
_WL_ENABLE = re.compile(
    rf"\b(?:open|enable|turn\s+on|switch\s+on|activate|start|set\s*up)\s+{_THE}waitlist\b|\bcollect\s+(?:emails?|sign-?ups?)\b", re.I)
_WL_CLOSE = re.compile(
    rf"\b(?:close|turn\s+off|switch\s+off|disable|shut\s+(?:off|down)|stop|deactivate)\s+{_THE}(?:waitlist|sign-?ups?)\b", re.I)
_WL_TEST = re.compile(rf"\b(?:test|probe|verify|try)\s+{_THE}waitlist(?:\s+form)?\b", re.I)
_REWRITE = re.compile(
    rf"\b(?:rewrite|redo|improve|polish|tighten|punch\s*up|refresh|rebuild|refine|update)\s+{_THE}(?:\w+\s+){{0,2}}(?:landing\s*page|web\s*site|website|site|page|homepage|copy)\b|"
    r"\bmake\s+(?:the\s+|my\s+|our\s+)?(?:headline|subhead\w*|tagline|cta|button|copy|landing\s*page)\b.{2,}|"
    r"\bchange\s+(?:the\s+|my\s+|our\s+)?(?:cta|call\s+to\s+action|button)\s+to\s+\S+", re.IGNORECASE)
_COMPONENT_REWRITE = re.compile(
    r"\b(?:change|set|replace|update|rewrite|edit|make)\s+(?:the\s+|my\s+|our\s+|your\s+)?"
    r"(?:headline|subhead\w*|tagline|cta|call\s+to\s+action|(?:(?:main|primary)\s+)?button(?:\s+(?:text|label))?)\b(?:\s+|[,:]\s*)\S+", re.I)
_TAKEDOWN = re.compile(
    rf"\bunpublish\b|\btake\s+(?:down\s+)?{_THE}(?:{_PAGE})\s+(?:down|offline)\b|\btake\s+{_THE}(?:{_PAGE})\s+off\s*line\b|"
    rf"\btake\s+(?:down\s+)?{_THE}{_PAGE}\b.{{0,12}}\boff\b|\b(?:shut\s+down|disable|remove|delete)\s+{_THE}(?:live\s+)?{_PAGE}\b", re.I)
_PREVIEW = re.compile(
    rf"\b(?:show|open|see|view|read|display|preview|pull\s+up|take\s+a\s+look\s+at)\b.{{0,20}}{_THE}(?:{_PAGE}|headline|copy)\b|"
    rf"\b(?:what|where)(?:'s|\s+is|\s+does)\b.{{0,20}}(?:{_PAGE}|headline)\b(?:\s+say)?", re.I)
_OTHER_STEER = re.compile(r"\b(?:competitors?|draft|e-?mail)\b|waitlist[\s-]*only", re.I)
_QUESTION_LEAD = re.compile(r"^\s*(?:how|why|when|where|is|are|does|do|did|should|has|have)\b", re.I)
_POLITE = re.compile(r"^\s*(?:can|could|would|will)\s+you\b", re.I)


_SYNTHETIC_NAMED_TEST = re.compile(
    r"^Test the existing (?P<name>[A-Za-z][A-Za-z0-9 -]{0,79}) waitlist with Supymem['’]s synthetic submission check\.\s+"
    r"Record this only as an internal functional test, never a real user or customer\.\s+"
    r"Do not publish the landing page or send any invitation\.?$", re.I)


def named_synthetic_waitlist_test(text: str) -> str | None:
    """One explicit internal-only probe; other compound requests stay generic."""
    match = _SYNTHETIC_NAMED_TEST.fullmatch((text or "").strip())
    return match['name'] if match else None


def builds_and_publishes(text: str) -> bool:
    """One request for the whole page: it stays one action, not a build, a publish and a waitlist card."""
    raw = (text or "").strip()
    return bool(_BUILD_AND_PUBLISH.search(raw)) and not _NEG.search(raw) and not _QUESTION_LEAD.match(raw) and not raw.endswith("?")


def classify_site_intent(text: str) -> str | None:
    """A SITE_INTENTS name; "assistant" when a publish/waitlist verb is negated or asked about; else None."""
    raw = (text or "").strip()
    rewrite = _REWRITE.search(raw) or _COMPONENT_REWRITE.search(raw)
    if not raw or not (_MENTIONS.search(raw) or rewrite or _PREVIEW.search(raw) or _TAKEDOWN.search(raw)):
        return None
    if named_synthetic_waitlist_test(raw):
        return "waitlist.test"
    if re.search(r"[.!?]\s+\S", raw):  # multi-sentence prompts keep their compound routing
        return None
    mentions_action = bool(_TAKEDOWN.search(raw) or _PUBLISH_WORD.search(raw) or _WL_ENABLE.search(raw) or _WL_CLOSE.search(raw))
    if (not mentions_action and not rewrite and not _WL_TEST.search(raw)
            and _PREVIEW.search(raw) and not _NEG.search(raw)
            and not re.search(r"\b(?:competitors?|e-?mail)\b|waitlist[\s-]*only", raw, re.I)):
        return "site.preview"
    if _QUESTION_LEAD.match(raw) or (raw.endswith("?") and not _POLITE.match(raw)):
        return "assistant" if mentions_action else None
    live = _NEG.sub(" ", raw).strip()
    if _TAKEDOWN.search(live):
        return "site.takedown"
    if _PUBLISH.search(live):
        return "site.publish"
    if _WL_CLOSE.search(live):
        return "waitlist.close"
    if _WL_ENABLE.search(live):
        return "waitlist.enable"
    if _WL_TEST.search(live):
        return "waitlist.test"
    if _OTHER_STEER.search(live):
        return None
    if _REWRITE.search(live) or _COMPONENT_REWRITE.search(live):
        return "site.rewrite"
    if _PREVIEW.search(live):
        return "site.preview"
    return "assistant" if mentions_action else None


def explicit_page_rewrite(text: str, *, literal: bool = False) -> bool:
    from app.chat.llm_router import _command_evidence
    from app.chat.product_build_request import _LOCAL_PREFIX, local_build_deferred

    source = _command_evidence(text)
    if local_build_deferred(source) or classify_site_intent(source) != "site.rewrite":
        return False
    start = re.match(_LOCAL_PREFIX, text, re.I).end()
    if not (_REWRITE.match(text, start) or _COMPONENT_REWRITE.match(text, start)):
        return False
    if not literal:
        return True
    from app.pagebuilder.qa import _copy_replacement, _copy_requests

    requests = _copy_requests(text)
    return len(requests) == 1 and _copy_replacement(text, requests[0]) is not None


# ---------- cards ----------

def waitlist_card(venture_id: str, *, label: str = "Open waitlist") -> dict:
    return replace(ConfirmationCard.for_action(
        get_action("waitlist.enable"),
        interpretation="Turn on the early-access waitlist for this venture.",
        consequences=[
            "Anyone with the waitlist link can submit an email address after seeing a consent line.",
            "Supymem saves each address for you, so you can contact them about early access.",
            "This does not publish your landing page. It only makes the waitlist form accept submissions.",
            _UNVERIFIED,
        ]), confirm_label=label).as_action()


def waitlist_close_card(signups: int) -> dict:
    return replace(ConfirmationCard.for_action(
        get_action("waitlist.close"),
        interpretation="Close the early-access waitlist for this venture.",
        consequences=[
            "The waitlist form stops accepting new email submissions, including from a published landing page.",
            f"The {signups} sign-up{'s' if signups != 1 else ''} already saved {'is' if signups == 1 else 'are'} kept. "
            "You can open the waitlist again any time.",
            "This does not un-publish or change your landing page.",
        ]), confirm_label="Close waitlist").as_action()


def publish_card(venture_id: str) -> dict:
    return replace(ConfirmationCard.for_action(
        get_action("site.publish"),
        interpretation="Publish your landing page to the public internet.",
        consequences=[
            "Anyone with the link can see it, and search engines may index it. Un-publishing later is a separate step.",
            "The landing page is queued for hosting; it is not live until the deploy finishes, and I will not say it is live before then.",
            "The waitlist form on it collects visitor email addresses under the consent line visitors see.",
            _UNVERIFIED,
        ]), confirm_label="Publish landing page").as_action()


# shortcut: copy generation keeps 600 instruction characters, lift this limit when it preserves longer instructions.
_REWRITE_INSTRUCTION_LIMIT = 600


def rewrite_card(instruction: str, *, label: str = "Rewrite landing page") -> dict:
    if len(instruction) > _REWRITE_INSTRUCTION_LIMIT:
        raise ValueError("site_rewrite_instruction_too_long")
    return replace(ConfirmationCard.for_action(
        get_action("site.rewrite"),
        interpretation=f"Rewrite the landing page copy: “{instruction[:160]}”",
        consequences=[
            "Saves a new draft version of the landing page. Nothing public changes and nothing is published.",
        ], resulting_state={"instruction": instruction}),
        confirm_label=label).as_action()


def landing_lane() -> str:
    """Always "build": landing pages are built by the founder's coding tool (Claude Code / Codex).

    The firstlook template lane is retired (founder directive 2026-10-05: no default template, not even as a
    fallback). SUPYMEM_LANDING_LANE is deliberately ignored; existing firstlook pages keep serving."""
    return "build"


def _reply(body: str, actions: list | None = None, kind: str = "text", *, edit_not_delivered=False) -> dict[str, Any]:
    from core.copy import no_dashes
    from app.core.local_mode import is_local
    if is_local():
        actions = [{**{key: value for key, value in action.items() if key != "href"},
                    "label": "Review your saved page", "action": "site.preview"}
                   if re.fullmatch(r"/v/[^/]+/site", action.get("href") or "") else action for action in actions or []]
        body = body.replace("Open Website", "Choose Review your saved page below").replace("open Website", "choose Review your saved page below")
    reply = {"kind": kind, "body_md": no_dashes(body), "actions": actions or [], "citations": []}
    if edit_not_delivered:
        reply["_edit_not_delivered"] = True
    return reply


def _local_page_setup(actor: str | None) -> dict | None:
    from app.core.local_mode import is_local

    if not is_local():
        return None
    from app.chat.execute import local_capability_reply

    if blocked := local_capability_reply("landing_pages"):
        return blocked
    from app.pagebuilder.tool import require_connected
    from fastapi import HTTPException

    try:
        require_connected(actor)
    except HTTPException as exc:
        if not isinstance(exc.detail, dict) or exc.detail.get("code") != "pagebuilder_coding_tool_required":
            raise
        return {**_reply(exc.detail["message"],
            [{"label": "Connect your coding tool", "href": "/settings/local#coding-tools", "style": "primary"}], "needs_you"),
            "action_started": False}
    return None


def _venture(venture_id: str) -> tuple[str | None, str, str]:
    from app.firstlook.service import _workspace_and_idea

    return _workspace_and_idea(venture_id)


def _summary(copy: dict) -> str:
    pts = [f"{i}. **{p.get('title', '')}**: {p.get('body', '')}" for i, p in enumerate(copy.get("points") or [], 1)]
    cta = copy.get("cta") or {}
    return "\n".join([f"**Headline:** {copy.get('headline', '')}", f"**Subhead:** {copy.get('subhead', '')}",
                      *pts, f"**Button:** {cta.get('label', '')}"])


def headline_summary(copy: dict) -> str:
    return f"“{copy.get('headline', '')}”: {copy.get('subhead', '')}"


# ---------- the live page is a deployed build, not the copy draft ----------

def live_page_note(venture_id: str, hint: str = "") -> tuple[str, list[dict]] | None:
    """When a page is live: say plainly that draft edits did not change it, and offer the real way to change it."""
    try:
        wid, _, _ = _venture(venture_id)
        live = _live_site(venture_id, wid)
    except Exception as exc:  # noqa: BLE001 - never let a note break a reply
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, venture_id, "site.live_page_note")
        return None
    if not live:
        return None
    return (f"Your live landing page at {live.get('url')} is a deployed build and has not changed. Copy edits here only update "
            "the draft on file. To change the live landing page itself it needs a new build, then a republish, and you confirm each step.",
            [{"label": "Prepare a build to change the live landing page", "action": "build.prepare",
              "payload": {"hint": (hint or "update the live landing page")[:200]}, "style": "secondary"}])


def takedown_reply(venture_id: str) -> dict[str, Any]:
    from app.chat.unpublish import prepare
    return prepare(venture_id)


# ---------- routing (intent -> card / needs_you) ----------

def _rewrite_unavailable(venture_id: str, instruction: str) -> dict | None:
    from app.firstlook import versions

    wid, _, _ = _venture(venture_id)
    latest = versions.get_latest(wid, venture_id=venture_id) if wid else None
    builds = _saved_page_builds(venture_id, wid)
    try:
        saved = _ready_page_build(builds) is not None
    except (KeyError, ValueError, TypeError):
        saved = False
    if builds and not saved:
        return _reply("You do not have a ready page to edit. Review your saved page, then build a version that passes its checks before trying again. No edit started and nothing public changed.\n\nYour requested changes:\n" + instruction,
                      [{"label": "Review your saved page", "action": "site.preview"}], "needs_you", edit_not_delivered=True)
    if not builds and latest is None:
        return _reply("You do not have a saved landing page to rewrite yet."
                      + " Review your idea, then build a version and review its private preview before publishing. "
                      "No rewrite started and nothing was charged.\n\nYour requested changes:\n" + instruction,
                      [{"label": "Review and edit your idea", "action": "idea.review"},
                       build_landing_button()],
                      "needs_you", edit_not_delivered=True)
    if len(instruction) > _REWRITE_INSTRUCTION_LIMIT:
        return _reply(f"Your rewrite instructions need to be {_REWRITE_INSTRUCTION_LIMIT} characters or fewer. "
                      "Please send a shorter version so none of your changes are dropped. No rewrite started and nothing was charged.",
                      [{"label": "Review your saved page", "action": "site.preview"}], "needs_you", edit_not_delivered=True)
    from pathlib import Path

    from app.pagebuilder.qa import literal_copy_edit

    try:
        before = _page_html(_ready_page_build(builds)) if builds else Path(latest.path).read_bytes()
        edit = literal_copy_edit(before, instruction, latest.copy if latest and not builds else None) if before else None
    except (OSError, UnicodeError):
        return _reply("Your saved page could not be read. Choose Review your saved page below and build a new version before trying that edit again.",
                      [{"label": "Review your saved page", "action": "site.preview"}], "needs_you", edit_not_delivered=True)
    except ValueError as exc:
        reason = ("Your requested edit would change the existing form. Your previous page stays saved. "
                  if str(exc) == "page_edit_form_changed" else "You cannot apply this edit to a single text element on your saved page. ")
        return _reply(reason + "Review your saved page, then send the exact text you want to replace outside the form. Nothing public changed.",
                      [{"label": "Review your saved page", "action": "site.preview"}], "needs_you", edit_not_delivered=True)
    if edit and edit[2] == before:
        return _reply("Your saved page already has that text. No edit started and your previous version stays saved. Review your saved page to choose another change.",
                      [{"label": "Review your saved page", "action": "site.preview"}], "site_preview", edit_not_delivered=True)
    return None


def route(venture_id: str, intent: str, text: str) -> dict[str, Any]:
    """Prepare exact scoped actions for the requested work."""
    if intent == "site.takedown":
        return takedown_reply(venture_id)
    if intent == "site.rewrite":
        from app.core.local_mode import is_local
        if is_local():
            from app.core.local_owner import current_owner
            if blocked := _rewrite_credential_reply(text):
                return blocked
            if setup := _local_page_setup(current_owner().id):
                return setup
        if unavailable := _rewrite_unavailable(venture_id, text.strip()):
            return unavailable
        return _reply("Choose Edit your landing page to apply your requested changes.",
                      [{"label": "Edit your landing page", "action": "site.rewrite",
                        "payload": {"instruction": text.strip()}, "style": "primary"}])
    if intent == "waitlist.enable":
        return _reply("Your waitlist collects visitor emails under a consent line. Check your Website for its status.",
                      [waitlist_card(venture_id)], "confirmation")
    if intent == "waitlist.close":
        state = _waitlist_state(venture_id)
        if not state.get("enabled"):
            return _reply("The waitlist is not on for this venture, so there is nothing to close. Nothing changed.",
                          [waitlist_card(venture_id)] if state.get("ok") else [])
        return _reply("I can close the waitlist. New sign-ups stop until you open it again, so please confirm.",
                      [waitlist_close_card(int(state.get("total") or 0))], "confirmation")
    if intent == "site.publish":
        from app.core.local_mode import is_local, is_sample_workspace
        if is_local() and builds_and_publishes(text):
            from app.core.local_owner import current_owner
            return publish_page(venture_id, current_owner().id)
        if is_local() and is_sample_workspace(_venture(venture_id)[0]):
            from app.core.local_owner import current_owner
            return _publish_local(venture_id, current_owner().id)
        _, build_id, blockers = publish_preflight(venture_id)
        if _builder_owns_publish(venture_id, build_id, blockers):
            return _reply("Your request is ready to run.", [{**publish_card(venture_id), "style": "primary"}], "confirmation")
        if not build_id:  # no accepted build: a firstlook concept page can still be published
            reply = firstlook_publish_reply(venture_id)
            if reply is not None:
                return reply
        if blockers:
            return _blocked(blockers)
        return _reply("Everything needed to publish is in place. Publishing makes the landing page public, so please confirm.",
                      [publish_card(venture_id)], "confirmation")
    raise ValueError(intent)


def _waitlist_state(venture_id: str) -> dict[str, Any]:
    """Managed waitlist status (the same source as the Website page); ok=False when it cannot be read."""
    from app.conversion import waitlist

    wid, _, _ = _venture(venture_id)
    try:
        return {**waitlist.status(venture_id, wid), "ok": True} if wid else {"ok": False}
    except waitlist.WaitlistError:
        return {"ok": False}


# ---------- firstlook concept page: publish through the same static hosting (hosting/firstlook_publish.py) ----------

_FL_BUTTONS = {
    # Already on but untested: offer the test itself, never the same "turn on" card again (that looped).
    "hosting_conversion_verification_required": lambda vid: (
        {"label": "Test waitlist", "action": "waitlist.test"} if _waitlist_state(vid).get("enabled")
        else waitlist_card(vid, label="Turn on / test the waitlist")),
    "hosting_firstlook_copy_rejected": lambda vid: rewrite_card("Rewrite the landing page so it only says what you are building, not what exists", label="Rewrite landing page"),
    "hosting_public_copy_unreviewed": lambda vid: rewrite_card("Remove placeholder or draft wording", label="Rewrite landing page"),
    "hosting_firstlook_page_rejected": lambda vid: rewrite_card("Rewrite the landing page", label="Rewrite landing page"),
}


def firstlook_publish_card(draft: dict) -> dict:
    return replace(ConfirmationCard.for_action(
        get_action("site.publish"),
        interpretation=f"Publish your concept landing page “{str(draft.get('headline') or '')[:120]}” to a public supymem.com address.",
        consequences=[
            "Anyone with the link can see it. It is a concept landing page: it says what you are building and has one button that opens your waitlist form.",
            "The exact landing page you are confirming (checked by fingerprint) is uploaded, then read back from the public address. I will not say it is live until that readback matches.",
            "Your waitlist test from the last 24 hours is re-checked just before the switch. Un-publishing later is a separate step.",
            _UNVERIFIED,
        ], resulting_state={"mode": "firstlook", "site_version_id": draft["site_version_id"],
                            "public_sha256": draft["sha256"]}), confirm_label="Publish landing page").as_action()


def firstlook_publish_reply(venture_id: str) -> dict[str, Any] | None:
    """The confirm card (or the blockers) for publishing the venture's firstlook draft; None when there is no such draft."""
    from app.core.local_mode import is_local
    from app.hosting import firstlook_publish as fp

    state = fp.describe(venture_id)
    if state["state"] == "live":
        return _reply(f"Your concept landing page is already live at {state['live_url']}. Nothing new was queued.",
                      [{"label": "Open Website", "href": f"/v/{venture_id}/site", "style": "primary"}])
    draft = state["draft"]
    codes = {blocker["code"] for blocker in state["blockers"]}
    uncertain = "hosting_public_state_uncertain" in codes
    review_saved_page = is_local() and (uncertain or "hosting_publication_in_progress" in codes)
    if draft is None and not uncertain and not review_saved_page and codes & {
        "hosting_app_not_found", "hosting_firstlook_draft_required", "hosting_firstlook_draft_unsupported"
    }:
        return None  # no firstlook draft: the build path decides
    lines = "\n".join(f"- {b['message']}" for b in state["blockers"])
    if state["blockers"]:
        buttons = [_FL_BUTTONS[c](venture_id) for c in dict.fromkeys(b["code"] for b in state["blockers"]) if c in _FL_BUTTONS]
        if review_saved_page:
            buttons.insert(0, {"label": "Open Website", "href": f"/v/{venture_id}/site"})
        introduction = ("The public state could not be confirmed. Open Website before publishing again. What needs review:\n"
                        if uncertain
                        else "I can't publish yet. Review the saved publishing status. What is missing:\n")
        return _reply(introduction + lines,
                      [{**a, "style": "primary" if i == 0 else "secondary"} for i, a in enumerate(buttons)], "needs_you")
    return _reply("Your concept landing page is ready to publish. Publishing makes it public, so please confirm.",
                  [firstlook_publish_card(draft)], "confirmation")


def _blocked(blockers: list[tuple[str, dict, str]]) -> dict[str, Any]:
    from app.chat.blockers import with_explain

    lines = "\n".join(f"- {t}" for t, _, _ in blockers)
    reply = _reply("I can't publish yet. Nothing went public. What is missing:\n" + lines,
                   [{**a, "style": "primary" if i == 0 else "secondary"} for i, (_, a, _) in enumerate(blockers)], "needs_you")
    return with_explain(reply, blockers[0][2]) if blockers else reply


def build_landing_button() -> dict:
    """Card-less button for the configured landing-page build."""
    from app.core.local_mode import is_local
    from app.pagebuilder.api import enabled

    if is_local() or enabled():
        return {"label": "Build landing page", "action": "pagebuilder.build", "payload": {"prepare": True},
                "style": "primary"}
    return {"label": "Build landing page", "action": "build.prepare", "payload": {"hint": "landing page"},
            "style": "primary"}


def _builder_publish_reply(venture_id: str, actor: str | None = None) -> dict | None:
    from fastapi import HTTPException
    from app.api.pagebuilder_api import profile
    from app.pagebuilder import api
    from app.tenancy import current_or_none

    scope = current_or_none()
    actor = actor or (scope.actor if scope and scope.venture_id == venture_id else None)
    if not api.enabled() or not actor or actor in {"system", "anonymous"}:
        return None
    wid, _, _ = _venture(venture_id)
    builds = _saved_page_builds(venture_id, wid)
    if not builds:
        return None
    try:
        current = profile(venture_id, actor).get("current")
    except HTTPException as exc:
        if exc.status_code != 409 or exc.detail != {"code": "pagebuilder_confirmed_revision_required"}:
            raise
        return _reply("Your saved landing page stays private. Review and confirm your current idea before publishing.",
                      [{"label": "Review your idea", "action": "idea.review"},
                       {"label": "Review your saved page", "action": "site.preview", "payload": {"job_id": builds[0].id}}],
                      "site_preview")
    if not current:
        return None
    publication = current.get("publication") or {}
    if publication.get("state") == "checking":
        body = "Your landing page is going through its final checks. It is not live yet. Review its saved publishing status below."
    elif publication.get("state") == "publishing":
        body = "Your landing page is being published, but this version is not live yet. Review its saved publishing status below."
    elif publication.get("state") == "live":
        body = "Your landing page is live. Review its saved publishing status and private preview below."
    elif publication.get("state") == "failed" or current["state"] == "failed":
        body = "Your latest landing page request stopped. Review its saved status and private preview below before trying again."
    elif current["state"] == "ready":
        body = ("Your saved landing page is ready for review and its preview is private. Review it and its publishing checks below. "
                "You must confirm before anything is published.")
    else:
        body = "Your landing page is still being built and its preview stays private. Review its status below before publishing."
    return _reply(body, [{"label": "Review your landing page", "action": "site.preview",
                         "payload": {"job_id": current["job_id"]}, "style": "primary"}], "site_preview")


def _builder_owns_publish(venture_id: str, build_id: str | None, blockers: list) -> bool:
    """Without an accepted build ready to deploy, the server page builder does the whole job."""
    from app.pagebuilder import api

    return api.enabled() and not (build_id and not blockers)


def _remember_publish(job_id: str) -> None:
    from app.db.models import Job
    from app.state import db

    with db()() as session:
        row = session.get(Job, job_id, with_for_update=True)
        doc = json.loads(row.payload_json)
        doc["publish_requested"] = True
        row.payload_json = json.dumps(doc)
        session.commit()


def _publish_saved_page(venture_id: str, actor: str, current: dict, profile_doc: dict) -> dict[str, Any]:
    """Queue this founder's ready page through its QA and hosting gates; the link is posted only once it is live."""
    from fastapi import HTTPException
    from app.builder.model_proxy import BuildModelError
    from app.builder.execution import BuildError
    from app.credits.errors import InsufficientCredits
    from app.hosting.service import HostingError
    from app.pagebuilder_admission import publication

    from app.core.local_mode import is_local
    if is_local():
        return _publish_local(venture_id, actor, job_id=current["job_id"])

    link = [{"label": "Review your landing page", "action": "site.preview", "payload": {"job_id": current["job_id"]}, "style": "secondary"}]
    saved = current.get("publication") or {}
    if saved.get("state") == "live":
        live = _live_site(venture_id, _venture(venture_id)[0])
        return _reply(f"Your landing page is already live at {live['url']}." if live else "Your landing page is already live.",
                      [{"label": "Open your page", "href": live["url"], "style": "primary"}] if live else link)
    candidates = current.get("server_candidates") or []
    if not candidates:
        return _reply("Your landing page is built, but it cannot go through final checks in this version. Nothing went public. Review it, then ask me to rebuild it.", link, "needs_you")
    selection = {"job_id": current["job_id"], "variant": candidates[0]["variant"],
                 "plan_id": profile_doc.get("plan_id"), "plan_task_id": profile_doc.get("task_id")}
    if not selection["plan_id"] or not selection["plan_task_id"]:
        selection["plan_id"] = selection["plan_task_id"] = None
    try:
        view = publication.request(venture_id, actor, selection)
    except InsufficientCredits:
        from app.chat.blockers import explain
        return explain(venture_id, {"code": "insufficient_credits"})
    except (BuildModelError, BuildError, HostingError, HTTPException, KeyError, PermissionError, ValueError) as exc:
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, venture_id, "site.publish")
        return _reply("I could not start publishing your landing page. Nothing went public. Try again, or review it first.",
                      [{"label": "Try again", "action": "site.publish", "retry": True, "style": "primary"}, *link], "needs_you")
    if view.get("state") == "live":
        live = _live_site(venture_id, _venture(venture_id)[0])
        return _reply(f"Your landing page is live at {live['url']}." if live else "Your landing page is live.",
                      [{"label": "Open your page", "href": live["url"], "style": "primary"}] if live else link)
    return _reply("Your landing page is built and is going through its final checks. It is not public yet. "
                  "I will publish it and post the link here when it is live.", link)


def publish_page(venture_id: str, actor: str) -> dict[str, Any]:
    """Build if needed, open the waitlist, publish: the founder asked for all of it once."""
    from fastapi import HTTPException
    from app.api.pagebuilder_api import profile
    from app.chat.execute import _pagebuilder_idea_card, _pagebuilder_quote_card
    from app.conversion import waitlist
    from app.pagebuilder import api

    wid, _, _ = _venture(venture_id)
    if not wid:
        raise KeyError("venture")
    from app.core.local_mode import is_local, is_sample_workspace

    if is_sample_workspace(wid) and not is_local():
        from app.pagebuilder.local_publish import build_test_preview

        return build_test_preview(venture_id, actor)
    if is_local():
        current = profile(venture_id, actor).get("current")
        if (not current or current["state"] == "failed") and (setup := _local_page_setup(actor)):
            return setup
    try:
        if not _waitlist_state(venture_id).get("enabled"):
            waitlist.configure(venture_id, wid, enabled=True, actor=actor)
    except waitlist.WaitlistError as exc:
        return _reply(f"I could not open your waitlist. {plain_waitlist(exc.code)} Nothing went public.",
                      [{"label": "Try again", "action": "site.publish", "retry": True, "style": "primary"}], "needs_you")
    try:
        profile_doc = profile(venture_id, actor)
        current = profile_doc.get("current")
        if current and current["state"] == "ready":
            return _publish_saved_page(venture_id, actor, current, profile_doc)
        if not current or current["state"] == "failed":
            quote = api.estimate(venture_id, actor)
            if not quote.get("ready"):
                return _pagebuilder_quote_card(venture_id, quote)
            current = api._start(venture_id, actor, expected_quote=quote["quote_fingerprint"], require_quote=True, retry_failed=True)
    except HTTPException as exc:
        if exc.status_code == 402:
            from app.chat.blockers import explain
            return explain(venture_id, {"code": "insufficient_credits"})
        if exc.status_code == 409 and exc.detail == {"code": "pagebuilder_confirmed_revision_required"}:
            return _pagebuilder_idea_card(venture_id, actor, publish=True)
        raise
    _remember_publish(current["job_id"])
    # The build can finish between the read and the mark: only then does nothing else carry the request forward.
    latest = profile(venture_id, actor, job_id=current["job_id"])
    if (latest.get("current") or {}).get("queue_state") == "succeeded" and latest["current"]["state"] == "ready":
        return _publish_saved_page(venture_id, actor, latest["current"], latest)
    from app.autopilot.receipts import queued_job
    queued_job(current["job_id"])
    return {"kind": "text", "run_id": current["job_id"], "citations": [], "actions": [],
            "body_md": ("Your coding tool is building your landing page with its waitlist form. " if is_local() else
                       "I'm building your landing page with its waitlist form. ") + "When it passes its final checks I will publish it "
                       "and post the link here. Nothing is public yet."}


def page_built(job_id: str) -> None:
    """After a build job completes: post the result, and continue a publish the founder already asked for. Never raises."""
    from app.chat.milestones import post_milestone
    from app.core.local_mode import is_local
    from app.db.models import Job
    from app.state import db

    try:
        with db()() as session:
            row = session.get(Job, job_id)
            venture_id = row.venture_id
            doc = json.loads(row.payload_json)
            actor = doc.get("_actor_subject")
            ready = doc["_runtime"]["state"] == "ready"
        if not venture_id or (not ready and not doc.get("rewrite")):
            return
        review = {"label": "Review your landing page", "action": "site.preview", "payload": {"job_id": job_id}, "style": "secondary"}
        if doc.get("rewrite"):
            reply = _saved_rewrite_reply(venture_id, row.workspace_id, job_id)
            post_milestone(venture_id, key=f"page_built:{job_id}", kind=reply["kind"],
                           body_md=reply["body_md"], actions=reply["actions"], requested=True)
            return
        if doc.get("publish_requested") and actor:
            from app.api.pagebuilder_api import profile
            latest = profile(venture_id, actor, job_id=job_id)
            reply = _publish_saved_page(venture_id, actor, latest["current"], latest)
            post_milestone(venture_id, key=f"page_built:{job_id}", kind="needs_you" if reply["kind"] == "needs_you" else "site_preview",
                           body_md=reply["body_md"], actions=reply["actions"], requested=True)
            return
        bundles = doc.get("result", {}).get("bundles") or []
        notice = bundles[0].get("manifest", {}).get("waitlist", {}).get("notice", "") if is_local() and bundles else ""
        post_milestone(venture_id, key=f"page_built:{job_id}", kind="site_preview", requested=True,
                       body_md="Your landing page preview is ready. It is private until you publish it."
                           + (" " + notice if notice else ""),
                       actions=[{"label": "Open preview", "href": f"/api/ventures/{venture_id}/pagebuilder/jobs/{job_id}/preview", "style": "primary"},
                                {"label": "Export", "href": f"/api/ventures/{venture_id}/pagebuilder/jobs/{job_id}/source"},
                                {"label": "Publish it", "action": "site.publish", "style": "secondary"}, review])
    except Exception as exc:  # noqa: BLE001 - the build itself already succeeded
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, None, "site.page_built")


def after_waitlist(venture_id: str, actor: str | None = None) -> tuple[str, list[dict]]:
    """Review saved page candidates before falling back to the accepted-build path."""
    try:
        if preview := _builder_publish_reply(venture_id, actor):
            return "Next: " + preview["body_md"], preview["actions"]
        _, build_id, blockers = publish_preflight(venture_id)
    except Exception as exc:  # noqa: BLE001 - when unsure, never offer to publish
        from app.chat.recovery import log_founder_exception
        log_founder_exception(exc, venture_id, "site.publish_status")
        return ("I could not check your landing page status. Check the saved page before building or publishing again.",
                [{"label": "Retry", "action": "site.preview", "payload": {}, "retry": True}])
    if build_id and not blockers:
        return "Next: publish.", [publish_card(venture_id)]
    if build_id:  # accepted build, but the waitlist is off or untested: that comes first, then publish
        off = blockers[0][2] == "waitlist_off"
        return (("Next: turn on the waitlist, test it, then publish." if off else "Next: test the waitlist, then publish."),
                [{**blockers[0][1], "style": "primary"}])
    return ("Next: build your landing page. There is nothing to publish until it is built and you accept it.",
            [build_landing_button()])


# ---------- publish preflight ----------

def publish_preflight(venture_id: str) -> tuple[str | None, str | None, list[tuple[str, dict, str]]]:
    """Return (app_id, accepted_build_id, blockers). Each blocker is (message, button, guide code)."""
    from sqlalchemy import select

    from app.conversion import waitlist
    from app.db.supymem_models import BuildRun
    from app.db.supymem_models import FounderApp
    from app.hosting.service import HostingError, _accepted_source
    from app.state import db

    wid, _, _ = _venture(venture_id)
    builds = {"label": "Open builds", "href": f"/v/{venture_id}/builds"}
    build_first = build_landing_button()
    blockers: list[tuple[str, dict, str]] = []
    app_id = build_id = None
    with db()() as session:
        # The Website page publishes the accepted build of ANY of the venture's apps; so does chat.
        # (Looking only at the oldest app missed an accepted build on a later one.)
        apps = session.scalars(select(FounderApp).where(FounderApp.venture_id == venture_id,
            FounderApp.deleted_at.is_(None)).order_by(FounderApp.created_at.asc(), FounderApp.id.asc())).all() if wid else []
        for app in apps:
            runs = session.scalars(select(BuildRun).where(BuildRun.app_id == app.id, BuildRun.state == "succeeded")
                                   .order_by(BuildRun.created_at.desc()).limit(10)).all()
            for run in runs:
                try:
                    _accepted_source(session, app, run.id)
                except HostingError:
                    continue
                app_id, build_id = app.id, run.id
                break
            if build_id:
                break
        if not apps:
            blockers.append(("There is no hosted app for this venture yet, so there is nothing to host. Build it first.", build_first, "no_hosted_app"))
        elif build_id is None:
            app_id = apps[0].id
            blockers.append(("No build has passed independent QA and your review yet. Publishing only ships an accepted build.", builds, "build_not_accepted"))
    try:
        state = waitlist.status(venture_id, wid) if wid else {}
    except waitlist.WaitlistError:
        state = {}
    if not state.get("enabled"):
        blockers.append(("The waitlist is off, and the landing page's sign-up button needs it.", waitlist_card(venture_id), "waitlist_off"))
    elif not state.get("submission_verified"):
        blockers.append(("The waitlist has no passing test in the last 24 hours. Publishing requires one.",
                         {"label": "Test waitlist", "action": "waitlist.test"}, "waitlist_test_failed"))
    return app_id, build_id, blockers


_LINK_MISSING = "The built landing page does not link to your waitlist form, so it cannot be published as is."
_QA_CHANGED = "This build no longer matches its accepted QA evidence. Review and accept the latest build first."
_HOST_DOWN = "Supymem hosting is temporarily unavailable. Your accepted build is still private and safe."
_HOSTING_MESSAGES = {
    "hosting_publication_interrupted": "The publish didn't go through and your public page is unchanged. You can publish again.",
    "hosting_conversion_link_required": _LINK_MISSING,
    "hosting_conversion_contract_unverified": _LINK_MISSING,
    "hosting_conversion_verification_required": "The waitlist test expired or failed. Run it again first.",
    "hosting_build_not_accepted": "That build is not accepted yet.",
    "hosting_source_receipt_invalid": _QA_CHANGED,
    "hosting_source_manifest_changed": _QA_CHANGED,
    "hosting_revision_not_approved": "The latest landing page revision has not been approved.",
    "hosting_public_copy_unreviewed": "The landing page copy has not been reviewed yet. Review it on the Website page first.",
    "hosting_provider_unavailable": _HOST_DOWN,
    "hosting_provider_configuration_invalid": _HOST_DOWN,
    "hosting_public_state_uncertain": "I could not confirm whether the public address was restored. Open Website before publishing again.",
}
_HOSTING_DEFAULT = "Publishing did not complete. Open Website to check the saved and public status before trying again."
_WAITLIST_MESSAGES = {
    "waitlist_enable_required": "The waitlist is off. Turn it on first.",
    "waitlist_probe_recent": "A test just ran. Wait a minute and try again.",
    "waitlist_not_found": "I could not find the waitlist for this venture.",
    "waitlist_public_submission_unconfirmed": "The public form did not confirm the test submission.",
    "waitlist_storage_receipt_missing": "I couldn't verify that your test signup was saved.",
    "waitlist_verification_required": "The waitlist has no passing test yet.",
}
_WAITLIST_DEFAULT = "Something went wrong on the waitlist side."


def plain_hosting(code: str) -> str:
    from app.core.local_mode import is_local
    if is_local():
        if code == "hosting_public_state_uncertain":
            return "I couldn't confirm whether your page is public. Open Website to check before publishing again."
        return code if code.startswith(("Open ", "Your ")) else "I couldn't confirm that your page was published. Open Website to check before trying again."
    return _HOSTING_MESSAGES.get(code, _HOSTING_DEFAULT)


def plain_waitlist(code: str) -> str:
    local_messages = {
        "waitlist_local_setup_required": "Open Settings > Keys and connections > Publishing and save your waitlist email, or choose Netlify for built-in forms.",
        "waitlist_local_publish_required": "Open Website and publish your page to your Netlify account, then choose Test waitlist again.",
        "waitlist_local_form_unconfirmed": "Open your Netlify site, check that waitlist.html is published, then retry Test waitlist from Website.",
    }
    if code in local_messages:
        return local_messages[code]
    message = _WAITLIST_MESSAGES.get(code, _WAITLIST_DEFAULT)
    from app.core.local_mode import is_local
    if is_local():
        return message + " Open Website > Collect signups to check your waitlist."
    return message


def _publish_local(venture_id, actor, *, job_id=None):
    from fastapi import HTTPException
    from app.hosting.service import HostingError
    from app.pagebuilder.local_publish import LocalPublishError, publish_site
    try:
        result = publish_site(venture_id, actor, job_id=job_id)
    except (LocalPublishError, HTTPException, HostingError) as error:
        message = str(error) if isinstance(error, LocalPublishError) else "Open Website, review your latest saved page, and try publishing again."
        return _reply(message, [{"label": "Open Website", "href": f"/v/{venture_id}/site"},
            {"label": "Keys and connections", "href": "/settings/local"}], "needs_you")
    return _reply(f"Your landing page is live at {result['url']}. {result['notice']}",
        [{"label": "Open your page", "href": result["url"], "style": "primary"}])


# ---------- executors ----------

async def execute(venture_id: str, action: str, payload: dict[str, Any], actor: str | None) -> dict[str, Any]:
    return await {"site.preview": _preview, "site.rewrite": _rewrite, "waitlist.enable": _enable,
                  "waitlist.close": _close, "waitlist.test": _test, "site.publish": _publish}[action](venture_id, payload, actor)


def _live_site(venture_id: str, wid: str | None) -> dict | None:
    """The live static deployment (same evidence the Website page uses), else None."""
    if not wid:
        return None
    from app.planning.publication_evidence import public_site_evidence
    from app.state import db

    with db()() as session:
        return public_site_evidence(session, workspace_id=wid, venture_id=venture_id)


def _saved_page_builds(venture_id: str, wid: str | None) -> list:
    from sqlalchemy import func, select
    from app.db.models import Job
    from app.pagebuilder import api
    from app.state import db

    with db()() as session:
        builds = session.scalars(select(Job).where(Job.kind == api.KIND, Job.workspace_id == wid,
            Job.venture_id == venture_id).order_by(func.coalesce(Job.finished_at, Job.started_at,
                Job.scheduled_for, Job.created_at).desc(), Job.created_at.desc(), Job.id.desc())).all() if wid else []
        builds.sort(key=lambda row: row.status in {"pending", "scheduled", "retry_wait", "leased", "running"}
            and json.loads(row.payload_json)["_runtime"]["state"] not in {"ready", "failed"}, reverse=True)
        for row in builds:
            session.expunge(row)
    return builds


def _build_progress(stage: object) -> str:
    stage = str(stage)
    if re.fullmatch(r"plan_repair_[1-5]", stage):
        return "refining the design"
    if re.fullmatch(r"capability_review_[1-6]", stage):
        return "checking the claims on your page"
    if re.fullmatch(r"repair_[12]", stage):
        return "checking your page"
    if re.fullmatch(r"image_[0-9]+", stage):
        return "creating the artwork"
    return {"brief_direction_plan": "designing your page", "code": "creating your page",
            "queued": "waiting to start"}.get(stage, "working on your page")


def ready_page_actions(venture_id: str) -> list[dict]:
    from sqlalchemy import func, select

    from app.core.local_mode import is_local
    from app.db.models import Job
    from app.pagebuilder import api
    from app.state import db

    if not is_local():
        return []
    workspace_id = _venture(venture_id)[0]
    if not workspace_id:
        return []
    with db()() as session:
        for row in session.scalars(select(Job).where(Job.kind == api.KIND, Job.workspace_id == workspace_id,
            Job.venture_id == venture_id).order_by(func.coalesce(Job.finished_at, Job.started_at,
                Job.scheduled_for, Job.created_at).desc(), Job.created_at.desc(), Job.id.desc())).yield_per(16):
            if api._state(row, json.loads(row.payload_json))[0] == "ready":
                prefix = f"/api/ventures/{venture_id}/pagebuilder/jobs/{row.id}"
                return [{"label": "Open preview", "href": prefix + "/preview", "payload": {"job_id": row.id}, "style": "primary"},
                        {"label": "Export", "href": prefix + "/source", "payload": {"job_id": row.id}, "style": "primary"}]
    return []


def refresh_page_actions(messages: list[dict], venture_id: str) -> list[dict]:
    from app.core.local_mode import is_local

    if not is_local():
        return messages
    for message in messages:
        if message.get("actions"):
            message["actions"] = [{"label": "Check your page status", "action": "site.preview"}
                                  if action.get("action") == "pagebuilder.build" and action.get("label") == "Retry"
                                  and (message.get("kind") == "site_preview" or action.get("retry") is True)
                                  and not (action.get("payload") or {}).get("retry_job_id") else action
                                  for action in message["actions"]]
    page_card = next((message for message in reversed(messages) if message.get("kind") == "site_preview"), None)
    next_card = next((message if message.get("kind") == "next_moves" else message["next_moves"]
                     for message in reversed(messages) if message.get("kind") == "next_moves" or message.get("next_moves")), None)
    if page_card is None and next_card is None:
        return messages
    actions = ready_page_actions(venture_id)
    prefix = f"/api/ventures/{venture_id}/pagebuilder/jobs/"
    for card in (page_card, next_card):
        if card is None:
            continue
        kept = [action for action in card.get("actions") or [] if not (action.get("href") or "").startswith(prefix)]
        if actions:
            kept = [{**action, "payload": {**(action.get("payload") or {}), **actions[0]["payload"]}}
                    if action.get("action") == "site.preview" else action for action in kept]
        card["actions"] = [*kept, *actions]
    return messages


async def _preview(venture_id, payload, actor):
    from app.core.local_mode import is_local
    from app.firstlook.service import current_site
    from app.pagebuilder import api

    wid, _, _ = _venture(venture_id)
    builds = _saved_page_builds(venture_id, wid)
    if builds:
        latest = api._view(builds[0])
        preview = latest if latest["state"] == "ready" else None
        for row in builds[1:]:
            if preview:
                break
            candidate = api._view(row)
            if candidate["state"] == "ready":
                preview = candidate
        if latest["state"] == "failed":
            from app.chat.execute import local_capability_reply

            setup = await asyncio.to_thread(local_capability_reply, "landing_pages")
            body = "Your latest landing page build stopped. " + latest["error_reason"]
            if setup:
                body = body.replace("choose Retry on Website", "check your setup below") + " " + setup["body_md"]
            else:
                body += " You can retry this build here."
        elif latest["state"] != "ready":
            progress = round(max(0, min(1, latest["progress"])) * 100)
            stage = _build_progress(latest["stage"])
            body = f"Your landing page build is {'queued' if latest['state'] == 'queued' else 'running'} ({progress}% complete, {stage}). Your preview stays private. This build has not published anything."
        else:
            body = "Your latest successful landing page build is ready."
        if preview:
            live = _live_site(venture_id, wid)
            body += (f" You have a live landing page at {live['url']}; the saved build preview is private."
                     if live else " Your preview is private, not published.")
            body += " Review it below, then publish it or edit your idea and rebuild here."
        controls = []
        if is_local():
            if latest.get("watch_url"):
                controls.append({"label": "Watch your tool", "href": latest["watch_url"], "payload": {"order_id": latest.get("order_id")}})
            if preview:
                prefix = f"/api/ventures/{venture_id}/pagebuilder/jobs/{preview['job_id']}"
                controls.extend([{"label": "Preview", "href": f"{prefix}/preview"}, {"label": "Export", "href": f"{prefix}/source"}])
            if latest["state"] == "failed":
                controls.extend(setup["actions"] if setup else [
                    {"label": "Retry", "action": "pagebuilder.build", "payload": {"retry_job_id": latest["job_id"]}, "style": "primary"}])
            body = body.replace("Open Build activity", "Choose Watch your tool below").replace("choose Retry on Website", "choose Retry below")
        return _reply(body, [{"label": "Refresh landing page status", "action": "site.preview",
                             "payload": {"job_id": (preview or latest)["job_id"]}},
                            {"label": "Edit your idea", "action": "idea.review"}, *controls], kind="site_preview")
    site = current_site(wid, venture_id=venture_id) if wid else None
    copy = (site or {}).get("copy") or {}
    live = _live_site(venture_id, wid)  # a static deployment is the published page; SiteVersion.published is legacy
    open_site = [{"label": "Open live site", "href": live["url"], "style": "primary"}] if live and live.get("url") else []
    if live and not copy.get("headline"):
        return _reply(f"Your landing page is live at {live['url']}.", open_site)
    if not copy.get("headline"):
        return _reply("You do not have a landing page yet. I can build a private preview for you here. Nothing is published.",
                      [{"label": "Build your landing page", "action": "pagebuilder.build", "payload": {"prepare": True}, "style": "primary"}])
    if live:
        return _reply(f"Your landing page is live at {live['url']}. The landing page itself comes from your accepted build; "
                      f"The copy draft on file (version {site.get('version')}):\n\n{_summary(copy)}",
                      [{"label": "Refresh private preview", "action": "site.preview",
                        "payload": {"workspace_id": wid, "version": site.get("version")}},
                       *open_site, rewrite_card("Tighten and improve the copy", label="Rewrite")], kind="site_preview")
    state = "public" if site.get("published") else "a draft preview, not public"
    return _reply(f"Your landing page (version {site.get('version')}, {state}):\n\n{_summary(copy)}",
                  [{"label": "Refresh private preview", "action": "site.preview",
                    "payload": {"workspace_id": wid, "version": site.get("version")}},
                   *open_site, rewrite_card("Tighten and improve the copy", label="Rewrite")], kind="site_preview")


def _page_html(row) -> bytes:
    return base64.b64decode(json.loads(row.payload_json)["result"]["bundles"][0]
                            .get("files", {}).get("index.html", ""), validate=True)


def _ready_page_build(builds):
    for row in builds:
        try:
            if json.loads(row.payload_json).get("_runtime", {}).get("state") == "ready" and _page_html(row):
                return row
        except (KeyError, ValueError, TypeError):
            continue
    return None


def _rewrite_watch_reply(current):
    if current.get("order_id") and current.get("watch_url") and current.get("state") in {"queued", "running", "critiquing"}:
        return {**_reply("Your page edit is queued with your coding tool. Choose Watch your page edit to see its progress or stop it. Nothing is published.",
            [{"label": "Watch your page edit", "href": current["watch_url"],
              "payload": {"order_id": current["order_id"]}, "style": "primary"}], "navigation"),
            "action_started": True, "_edit_job_id": current["job_id"]}
    return None


def _saved_rewrite_reply(venture_id, workspace_id, job_id):
    from app.db.models import Job
    from app.pagebuilder import api
    from app.pagebuilder.qa import edit_applied
    from app.state import db

    review = [{"label": "Review your saved page", "action": "site.preview", "payload": {"job_id": job_id}}]
    with db()() as session:
        saved = session.get(Job, job_id)
        if not saved or (saved.kind, saved.venture_id, saved.workspace_id) != (api.KIND, venture_id, workspace_id):
            raise ValueError("pagebuilder_not_found")
        doc = json.loads(saved.payload_json)
        if doc["_runtime"]["state"] == "failed":
            review[0]["payload"]["job_id"] = doc["rewrite"]["source_job_id"]
            confirmed = not any(operation.get("state") in {"calling", "uncertain"}
                for operation in doc["_runtime"].get("operations", {}).values())
            from app.core.local_mode import is_local

            if is_local() and not confirmed:
                return _reply("Your earlier page edit could not be confirmed. Open Website and compare your saved edit with your private preview before retrying. Nothing public changed.",
                              [*review, {"label": "Open Website", "href": f"/v/{venture_id}/site", "kind": "recovery_link"}],
                              "needs_you")
            return _reply("Your page edit could not be applied. Review your saved page, then try again with the exact text you want to replace. Nothing public changed.", review, "needs_you", edit_not_delivered=confirmed)
        if doc["_runtime"]["state"] != "ready":
            from app.core.local_mode import is_local

            if is_local():
                from app.pagebuilder.tool import progress

                if watch := _rewrite_watch_reply({"job_id": job_id, **progress(saved, doc)}):
                    return watch
            return _reply("Your page edit is still being checked. Nothing public changed. Review your saved page when it finishes.", review)
        source = session.get(Job, doc["rewrite"]["source_job_id"])
        if not source or (source.kind, source.venture_id, source.workspace_id) != (api.KIND, venture_id, workspace_id):
            raise ValueError("pagebuilder_not_found")
        before, after = _page_html(source), _page_html(saved)
        fallback = doc["result"]["bundles"][0].get("manifest", {}).get("repair_fallback")
    applied = not fallback and edit_applied(before, after, doc["rewrite"]["instruction"])
    if not applied:
        return _reply("Your saved page did not change as requested. Review your saved page, then send the exact text you want to replace. Nothing public changed.", review, "needs_you")
    return _reply("Your saved landing page changed. Review your private preview before publishing; nothing public changed.", review, "site_preview")


def _rewrite_credential_reply(instruction):
    from app.secrets.detect import looks_like_secret

    if looks_like_secret(instruction):
        return _reply("Remove credentials from your page edit. Save keys in Settings > Keys and connections, then send the edit again without credentials.",
                      [{"label": "Open Keys and connections", "href": "/settings/local", "kind": "recovery_link"}], "needs_you")
    return None


async def _rewrite(venture_id, payload, actor):
    from app.core.local_mode import is_local
    from app.firstlook import versions
    from app.firstlook.service import generate_first_look

    instruction = str(payload.get("instruction") or payload.get("query") or "").strip()
    if is_local() and (blocked := _rewrite_credential_reply(instruction)):
        return blocked
    if setup := await asyncio.to_thread(_local_page_setup, actor):
        return setup
    if unavailable := _rewrite_unavailable(venture_id, instruction):
        return unavailable
    wid, idea, name = _venture(venture_id)
    builds = _saved_page_builds(venture_id, wid)
    if builds:
        from app.pagebuilder import api
        try:
            source_id = _ready_page_build(builds).id
            current = await asyncio.to_thread(api.rewrite, venture_id, actor,
                new_version_of=source_id, instruction=instruction)
            if is_local() and (watch := _rewrite_watch_reply(current)):
                return watch
            reply = await asyncio.to_thread(_saved_rewrite_reply, venture_id, wid, current["job_id"])
            reply["_edit_job_id"] = current["job_id"]
            return reply
        except Exception as exc:  # noqa: BLE001
            from fastapi import HTTPException
            if isinstance(exc, HTTPException):
                if is_local() and isinstance(exc.detail, dict) and exc.detail.get("code") == "pagebuilder_coding_tool_required":
                    return _reply(exc.detail["message"], [{"label": "Connect your coding tool",
                        "href": "/settings/local#coding-tools", "style": "primary"}], "needs_you")
                if isinstance(exc.detail, dict) and exc.detail.get("code") == "pagebuilder_outcome_uncertain":
                    if is_local():
                        for saved in _saved_page_builds(venture_id, wid):
                            document = json.loads(saved.payload_json)
                            rewrite = document.get("rewrite", {})
                            runtime = document.get("_runtime", {})
                            unresolved = runtime.get("state") != "failed" or any(
                                operation.get("state") in {"calling", "uncertain"}
                                for operation in runtime.get("operations", {}).values())
                            if unresolved and rewrite.get("instruction") == instruction and rewrite.get("source_job_id") == source_id:
                                return _saved_rewrite_reply(venture_id, wid, saved.id)
                        return _reply("Your earlier page edit could not be confirmed. Open Website and compare your saved edit with your private preview before retrying. Nothing public changed.",
                                      [{"label": "Open Website", "href": f"/v/{venture_id}/site", "kind": "recovery_link"}],
                                      "needs_you")
                    return _reply("Your earlier page edit could not be confirmed. Review your saved page, then contact support before retrying. Nothing public changed.",
                                  [{"label": "Review your saved page", "action": "site.preview"}], "needs_you")
                if exc.status_code == 402:
                    from app.chat.blockers import explain
                    return explain(venture_id, {"code": "insufficient_credits"})
                if isinstance(exc.detail, dict) and exc.detail.get("code") in {"pagebuilder_build_limit", "pagebuilder_attempt_limit"}:
                    return _reply("You have reached the page-build limit for this idea. Review your saved page, then make further edits in your coding tool. Nothing public changed.",
                                  [{"label": "Review your saved page", "action": "site.preview"}], "needs_you")
                return _reply("Your page edit could not start. Review your saved page and wait for any current edit to finish, then try again. Nothing public changed.",
                              [{"label": "Review your saved page", "action": "site.preview"}], "needs_you")
            from app.chat.recovery import founder_failure
            return founder_failure(exc, venture_id, "your landing page edit", {"action": "site.preview", "payload": {}},
                safety="Review your saved page before trying again. Nothing was published by this request.")
    latest = versions.get_latest(wid, venture_id=venture_id) if wid else None
    if latest is None:
        return _reply("I could not find your saved draft to rewrite. Review your landing page before trying again. "
                      "No rewrite started and nothing was charged.",
                      [{"label": "Review your saved page", "action": "site.preview"}], "needs_you")
    cur = latest.copy
    from pathlib import Path
    fresh = {"archetype": latest.archetype, "stage": latest.stage,
             "guidance": (f"{instruction or 'Tighten and improve the copy.'} Current headline: {cur.get('headline', '')} "
                          f"Current subhead: {cur.get('subhead', '')}")}
    try:
        from app.pagebuilder import jobs
        from app.pagebuilder.qa import edit_applied
        receipts = []
        if is_local():
            from app.firstlook.service import prepare_local_page

            order = await asyncio.to_thread(prepare_local_page, wid, venture_id,
                {"idea": idea, "name": name, "request": instruction}, source=latest)
            return {**_reply("Your page edit is queued with your coding tool. Choose Watch your page edit to see its progress or stop it. Nothing is published.",
                [{"label": "Watch your page edit", "href": order["review_path"],
                  "payload": {"order_id": order["id"]}, "style": "primary"}], "navigation"),
                "action_started": True, "device_order": order}
        before = Path(latest.path).read_bytes()
        receipt_token = jobs.EDIT_RECEIPTS.set(receipts)
        try:
            view = await generate_first_look(wid, idea, venture_id=venture_id, venture_name=name,
                archetype=fresh["archetype"], stage=fresh["stage"], instruction=fresh["guidance"],
                edit_source=latest, edit_instruction=instruction)
        finally:
            jobs.EDIT_RECEIPTS.reset(receipt_token)
        from app.core.local_mode import is_local
        if is_local() and isinstance(view, dict):
            return {**_reply("Your coding tool is working on your requested page changes. Open Builds to check its progress.",
                [{"label": "Open Builds", "href": view["review_path"]}]), "device_order": view}
        saved = versions.get_latest(wid, venture_id=venture_id)
        changed = saved is not None and edit_applied(before, Path(saved.path).read_bytes(), instruction)
    except Exception as exc:  # noqa: BLE001 - an unconfirmed provider outcome stays an honest pause
        from llm_gateway.errors import ProviderCallError

        from app.firstlook.service import FirstLookError

        if is_local() and isinstance(exc, FirstLookError):
            return _reply(exc.message, [{"label": "Review your saved page", "action": "site.preview"}], "needs_you")
        if isinstance(exc, FirstLookError) and exc.code in {"page_edit_form_changed", "page_edit_unchanged", "page_edit_target_unavailable"}:
            from app.state import db
            with db()() as session:
                for usage in receipts:
                    jobs.refund_edit_usage(usage, wid, venture_id, session, latest.id)
                session.commit()
            reason = ("Your rewrite would change the existing form, including its submission or consent details. "
                      if exc.code == "page_edit_form_changed" else "Nothing changed. Your requested edit could not be applied. ")
            return _reply(reason + "Your previous page stays saved. Review your saved page, then send the exact text you want to replace. Your requested rewrite is not public.",
                          [{"label": "Review your saved page", "action": "site.preview"}], "needs_you", edit_not_delivered=True)

        from app.chat.recovery import founder_failure
        from app.llm.provider_errors import provider_error_for_user

        safety = "Check the saved draft before another write. Nothing was published by this request."
        failure = founder_failure(exc, venture_id, "your landing page rewrite",
            {"action": "site.preview", "payload": {}},
            safety=safety)
        if isinstance(exc, ProviderCallError) and exc.code == "provider_outcome_unknown":
            error = provider_error_for_user(exc)
            failure["body_md"] = error["message"] + " " + safety + " " + error["hint"]
            failure["actions"] = []
        return {**failure, "kind": "needs_you"}
    status = ("Your saved landing page changed. Review your private preview before publishing." if changed else
              "Your saved page did not change as requested. Review it, then send the exact text you want to replace.")
    return _reply(f"{status} (version {view.version}, draft only, not public).\n\n{_summary(view.copy)}",
                  [{"label": "Refresh private preview", "action": "site.preview",
                    "payload": {"workspace_id": wid, "version": view.version}},
                   waitlist_card(venture_id)], kind="site_preview")


async def _enable(venture_id, payload, actor):
    from app.conversion import waitlist

    wid, _, _ = _venture(venture_id)
    if not wid:
        raise KeyError("venture")
    try:
        state = await asyncio.to_thread(waitlist.configure, venture_id, wid, enabled=True, actor=actor or "chat")
    except waitlist.WaitlistError as exc:
        return _reply(f"I could not turn the waitlist on. {plain_waitlist(exc.code)} Nothing changed.")
    reused = bool(state.get("enabled") and state.get("submission_verified"))
    ok, why = reused, _WAITLIST_MESSAGES["waitlist_storage_receipt_missing"]
    destination_tested = False
    if not reused:
        try:
            probed = await asyncio.to_thread(waitlist.probe, venture_id, wid)
            ok = bool(probed.get("submission_verified"))
            destination_tested = probed.get("destination_tested") is True
            why = str(probed.get("note") or why)
        except waitlist.WaitlistError as exc:
            ok, why = False, plain_waitlist(exc.code)
    nxt, actions = await asyncio.to_thread(after_waitlist, venture_id, actor)
    result = ("Your waitlist is already on and its saved successful test is still valid. No new test ran." if reused else
              "Waitlist on and tested ✓ A synthetic submission (no real email) went through the public form and was stored; "
              "it is not counted as a sign-up." if ok else why + " Nothing counted as verified." if destination_tested else
              f"The waitlist test did not pass: {why} Nothing counted as verified.")
    form = {"label": "Open your waitlist form", "href": state["url"], "style": "secondary"}
    return _reply("Waitlist is on. " + nxt + " " + result + " " + _UNVERIFIED,
                  [*(actions if ok or any(a.get("action") == "waitlist.test" for a in actions)
                     else actions + [{"label": "Test waitlist", "action": "waitlist.test", "style": "secondary"}]), form],
                  kind="text" if ok else "needs_you")


async def _close(venture_id, payload, actor):
    from app.conversion import waitlist

    wid, _, _ = _venture(venture_id)
    if not wid:
        raise KeyError("venture")
    try:
        before = await asyncio.to_thread(waitlist.status, venture_id, wid)
        if not before.get("enabled"):
            return _reply("The waitlist was already off, so nothing changed.", [waitlist_card(venture_id)])
        state = await asyncio.to_thread(waitlist.configure, venture_id, wid, enabled=False, actor=actor or "chat")
    except waitlist.WaitlistError as exc:
        return _reply(f"I could not close the waitlist. {plain_waitlist(exc.code)} Nothing changed.")
    if state.get("enabled"):  # never claim a change the service did not make
        return _reply("I asked to close the waitlist but it still shows as on. Say \"close the waitlist\" to try again.",
                      [{"label": "Open Website", "href": f"/v/{venture_id}/site", "style": "primary"}])
    n = int(state.get("total") or 0)
    return _reply(f"The waitlist is closed. It no longer accepts new sign-ups, and the {n} already saved "
                  f"{'is' if n == 1 else 'are'} kept. You can open it again any time.",
                  [waitlist_card(venture_id, label="Open waitlist again")])


async def _test(venture_id, payload, actor):
    from app.chat.blockers import with_explain
    from app.conversion import waitlist

    wid, _, name = _venture(venture_id)
    if not wid:
        raise KeyError("venture")
    target = named_synthetic_waitlist_test(str(payload.get("query") or ""))
    scoped = target is not None
    website = [{"label": "Open Website", "href": f"/v/{venture_id}/site", "style": "primary"}]
    if scoped and re.sub(r"[ -]", "", target).casefold() != re.sub(r"[ -]", "", name).casefold():
        return _reply("That named waitlist does not match this venture. No test ran. Open this venture's Website and use Test public submission for its existing waitlist.", website, "needs_you")
    try:
        state = await asyncio.to_thread(waitlist.probe, venture_id, wid)
    except waitlist.WaitlistError as exc:
        why = (plain_waitlist(exc.code) if exc.code in {"waitlist_enable_required", "waitlist_probe_recent"}
               else f"The test did not complete. {plain_waitlist(exc.code)} Nothing counted as verified.")
        off = exc.code == "waitlist_enable_required"
        return with_explain(_reply(why, website if scoped else [waitlist_card(venture_id)] if off else [], "needs_you"),
                            "waitlist_off" if off else "waitlist_test_failed")
    if state.get("destination_tested"):
        return _reply(state["note"], [{"label": "Open your waitlist", "href": state["url"]},
            {"label": "Collect signups", "href": f"/v/{venture_id}/site"}])
    ok = state.get("submission_verified")
    reply = _reply(("The waitlist test passed: a synthetic submission (no real email) went through the public form and was stored. "
                   "It is not counted as a sign-up." if ok else "The test ran but no stored receipt was confirmed, so the waitlist is not verified."),
                  website if scoped else (await asyncio.to_thread(after_waitlist, venture_id, actor))[1] if ok else [], "text" if ok else "needs_you")
    return reply if ok else with_explain(reply, "waitlist_test_failed")


async def _publish(venture_id, payload, actor):
    from app.hosting.service import HostingError, queue_accepted_build
    from app.chat.milestone_hooks import started_reply

    if not actor:
        raise PermissionError("publish_chat_authentication_required")
    from app.core.local_mode import is_local
    if is_local():
        from app.pagebuilder.local_publish import enabled, blocked_message
        if not enabled("publish_pages"):
            return _reply(blocked_message("publish_pages"), [{"label": "Export your page", "href": f"/v/{venture_id}/site"},
                {"label": "Keys and connections", "href": "/settings/local"}], "needs_you")
        if payload.get("job_id") or payload.get("mode") == "firstlook":
            return await asyncio.to_thread(_publish_local, venture_id, actor, job_id=payload.get("job_id"))
        from app.api.pagebuilder_api import profile
        from fastapi import HTTPException
        try:
            current = (await asyncio.to_thread(profile, venture_id, actor)).get("current")
        except HTTPException as error:
            if error.status_code != 409:
                raise
            current = None
        if current and current.get("state") == "ready":
            return await asyncio.to_thread(_publish_local, venture_id, actor, job_id=current["job_id"])
        from app.firstlook import versions
        wid = _venture(venture_id)[0]
        if wid and await asyncio.to_thread(versions.get_latest, wid, venture_id=venture_id):
            return await asyncio.to_thread(_publish_local, venture_id, actor)
        return await asyncio.to_thread(publish_page, venture_id, actor)
    if payload.get("mode") == "firstlook":
        from app.hosting import firstlook_publish as fp

        try:
            dep = await asyncio.to_thread(fp.queue_publication, venture_id, actor=actor,
                                          site_version_id=str(payload.get("site_version_id") or ""),
                                          public_sha256=str(payload.get("public_sha256") or ""))
        except HostingError as exc:
            return _reply(fp.plain(exc.code))
        link = [{"label": "Open site", "href": f"/v/{venture_id}/site", "style": "primary"}]
        if dep.get("state") == "live":
            return _reply("This landing page is already live. Nothing new was queued.", link)
        return started_reply(_reply("Publishing is queued (not live yet). I will say it is live only after the public address serves "
                      "exactly this landing page.", link), dep.get("job_id"))
    app_id, build_id, blockers = publish_preflight(venture_id)
    if _builder_owns_publish(venture_id, build_id, blockers):
        from app.chat.confirmations import start_requested_work

        reply = await asyncio.to_thread(publish_page, venture_id, actor)
        return await start_requested_work(venture_id, reply, actor, actions={"idea.confirm"})
    if blockers or not (app_id and build_id):
        return _blocked(blockers)
    try:
        dep = await asyncio.to_thread(queue_accepted_build, app_id, build_id, actor=actor)
    except HostingError as exc:
        return _reply(plain_hosting(exc.code) + " Nothing went public.")
    link = [{"label": "Open site", "href": f"/v/{venture_id}/site", "style": "primary"}]
    if dep.get("state") == "live":
        return _reply("This build is already live. Nothing new was queued.", link)
    return started_reply(_reply("Publishing is queued (not live yet). I will not call it live until the deploy finishes.", link), dep.get("job_id"))
