"""Never claim an action happened unless one did.

Free-form model replies (the assistant and general paths) cannot start anything: a build, a launch,
a publish or a send only happens after a founder taps a confirmation card. If such a reply says it
did (or is doing) one anyway, it is replaced with an honest offer and the real button.
"""
from __future__ import annotations

import re
from typing import Any

# First-person "I kicked it off / I'll start the build / I've published" and "now starts the build".
_CLAIM = re.compile(
    r"\bI(?:['’]ll|\s+will|['’]m|\s+am|['’]ve|\s+have)?\s+(?:just\s+|now\s+|already\s+|go\s+ahead\s+and\s+|going\s+to\s+)*"
    r"(?:kick(?:ed|ing)?\s+(?:it|that|this|things|the\s+\w+(?:\s+\w+)?)?\s*off|"
    r"built|build(?:ing)?|start(?:ed|ing)?(?!\s+(?:by|with)\b)|launch(?:ed|ing)?|publish(?:ed|ing)?|deploy(?:ed|ing)?|"
    r"queue[d]?|queuing|trigger(?:ed|ing)?|sen[dt](?:ing)?(?!\s+you\b))\b"
    r"|\bnow\s+starts?\s+the\s+build\b|\bthe\s+build\s+(?:is\s+)?(?:now\s+)?(?:starting|underway|kicked\s+off)\b",
    re.I)
# A persona or settings change ("I will respond as Jarvis from now on", "the waitlist has been closed") is only
# real after a confirmed card ran; a free-form reply cannot make one.
_SETTING_CLAIM = re.compile(
    r"\bI(?:'ll|\s+will|'m\s+going\s+to)?\s+(?:respond|answer|reply|go|be\s+known)\s+as\s+[A-Z]\w*|"
    r"\bfrom\s+now\s+on,?\s+I\b|\bI(?:'ve|\s+have)?\s+(?:just\s+)?(?:renamed|changed\s+my\s+name|updated\s+my\s+name)\b|"
    r"\b(?:waitlist|sign-?ups?)\s+(?:has\s+been|is\s+now|was|have\s+been)\s+(?:closed|turned\s+(?:off|on)|disabled|opened|enabled)\b|"
    r"\bI(?:'ve|\s+have)?\s+(?:just\s+)?(?:closed|disabled|turned\s+(?:off|on))\s+(?:the\s+)?(?:waitlist|sign-?ups?)\b",
    re.I)
# Preparing a durable execution review is an effect too. Inline prose drafts,
# outlines and examples are ordinary writing and do not claim a saved review.
_PREPARED_REVIEW_CLAIM = re.compile(
    r"\bI(?:['’]ve|\s+have|['’]m|\s+am)?\s+(?:just\s+|now\s+|already\s+)*"
    r"(?:prepared|preparing)\s+(?:(?:a|an|the|your|that|this)\s+)?"
    r"(?:(?:new|saved|requested|reviewed|approved|read-only|read only|background|Mac|browser|coding)\s+){0,6}"
    r"(?:task|work[- ]order|review\s+card|confirmation\s+card|(?:build|execution|coding)\s+(?:quote|review))\b"
    r"(?![\s-]+(?:list|outline|description|draft|example|text|summary)\b)",
    re.I)
_BUILD_WORDS = re.compile(r"\b(?:built|build(?:ing)?|kick(?:ed|ing)?\s+(?:it\s+)?off|landing|launch(?:ed|ing)?)\b", re.I)
_PUBLISH_WORDS = re.compile(r"\b(?:publish\w*|deploy\w*|go(?:ing)?\s+live)\b", re.I)


def claims_action(body: str) -> bool:
    # ponytail: sentence-local conditional promises, use structured action claims for ambiguous prose.
    action_claim = False
    for sentence in re.split(r"[.!?\n]+", body or ""):
        for claim in _CLAIM.finditer(sentence):
            # "I'm built into Supymem" describes the assistant, not an action it took.
            if re.match(r"I(?:['’]m|\s+am)\s+(?:just\s+|now\s+|already\s+)*built\b", claim.group(), re.I):
                continue
            prospective = re.match(r"I(?:['’]ll|\s+will|['’]m\s+going\s+to|\s+am\s+going\s+to)\b", claim.group(), re.I)
            conditional = re.search(r"\b(?:if|once|when)\b", sentence[:claim.start()], re.I) or re.search(
                r"\b(?:once|after|when|if)\s+you\s+(?:confirm|approve)\b", sentence[claim.end():], re.I)
            if not (prospective and conditional):
                action_claim = True
    return bool(action_claim or _SETTING_CLAIM.search(body or "")
                or _PREPARED_REVIEW_CLAIM.search(body or ""))


def honest_reply(venture_id: str, body: str) -> dict[str, Any]:
    """What the founder actually gets: nothing ran, here is the button that would run it."""
    actions: list[dict[str, Any]] = []
    if _PREPARED_REVIEW_CLAIM.search(body):
        return {"kind": "text", "body_md": (
            "This reply has not prepared a new saved task review. A preparation is real only when Supymem "
            "returns its saved review card. Open Settings → computer use to check the available task profiles, "
            "or ask for the reviewed live-site/waitlist check. Existing tasks keep their saved state."
        ), "actions": [], "citations": []}
    if _SETTING_CLAIM.search(body) and not _CLAIM.search(body):
        offer = ("I have not changed anything: no name, setting or waitlist was changed by that reply. "
                 "A change only happens after you confirm a card. To rename me, say \"call yourself <name>\"; "
                 "to close the waitlist, say \"close the waitlist\".")
        return {"kind": "text", "body_md": offer, "actions": [], "citations": []}
    if _PUBLISH_WORDS.search(body) and not _BUILD_WORDS.search(body):
        offer = "I have not published anything. Publishing happens only after you confirm it."
        from app.chat.site_actions import publish_card

        actions.append(publish_card(venture_id))
    elif _BUILD_WORDS.search(body):
        offer = "I have not started the build yet. You can start the next build step here."
        actions.append({"label": "Review and start the build", "action": "build.prepare", "style": "primary"})
    else:
        from app.chat.handlers.general import _next_step_actions

        actions.extend(_next_step_actions(venture_id))
        offer = (f"I have not started anything yet. Your next step is: {actions[0]['label']}. Use that button below."
                 if actions else "I have not started anything yet. Ask me what you should do next.")
    return {"kind": "text", "body_md": offer, "actions": actions, "citations": []}


# A dead end ("I can't click that", "only you can press it") must always come with another way forward.
_DEAD_END = re.compile(r"\bI\s+(?:can(?:'|\u2019)?t|cannot|am\s+not\s+able\s+to|'m\s+unable\s+to)\s+(?:click|press|tap|do\s+(?:that|this))\b"
                       r"|\bonly\s+you\s+can\s+(?:press|click|tap)\b", re.I)


def with_alternative(reply: dict[str, Any]) -> dict[str, Any]:
    """Add "Do it another way" to a reply that says Charlie cannot do something and offers nothing."""
    from app.chat import recovery

    actions = list(reply.get("actions") or [])
    if not _DEAD_END.search(str(reply.get("body_md") or "")) or any(
            isinstance(a, dict) and a.get("action") == recovery.ALTERNATIVE for a in actions):
        return reply
    return {**reply, "actions": actions + [recovery.button("chat.action", "chat_reply")]}
