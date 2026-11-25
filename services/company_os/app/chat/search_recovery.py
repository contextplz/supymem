"""Read-only manual search alternatives; no model, search or outreach effects."""
from __future__ import annotations

import re
from urllib.parse import urlencode


_EFFECT_VERB = r"(?:send|search|build|publish|post|deploy|schedule|contact|run a Mac task)"
_NEGATED_EFFECT_LIST = re.compile(
    r"\b(?:do not|don['’]t|never)\s+(?P<verbs>" + _EFFECT_VERB
    + r"(?:\s*,\s*" + _EFFECT_VERB + r")+"
    + r"(?:\s*,?\s+(?:or|and)\s+" + _EFFECT_VERB + r")?)"
    + r"(?:\s+(?:anything|it|them))?(?=\s*(?:[.!?;]|$))", re.I)


def without_negated_effect_lists(text: str) -> str:
    """Remove complete supported prohibition lists, never mixed commands."""
    return _NEGATED_EFFECT_LIST.sub(" ", text)


def search_review_first(text: str) -> bool:
    # shortcut: explicit approval-first wording only, use grounded action order for compound instructions.
    return bool(re.search(r"(?:^|[.!?;\n])\s*(?:but\s+)?first\s+(?:tell|show|explain|describe|outline)\b"
                          r"[^.!?;\n]{0,180}\b(?:approval|permission|consent)\b", text, re.I))


def search_forbidden(text: str) -> bool:
    return bool(
        any(re.search(r"\bsearch\b", match['verbs'], re.I)
            for match in _NEGATED_EFFECT_LIST.finditer(text))
        or search_review_first(text)
        or re.search(r"\b(?:do not|don['’]t|without|no)\s+(?:(?:perform|run|use|call|do|start)\s+)?(?:a\s+|any\s+)?(?:web[- ]?|online\s+)?search\b", text, re.I)
        or re.search(r"\b(?:do not|don['’]t|without|no)\b[^.!?;]{0,60}\b(?:web[- ]?search|search tools?|call[^.!?;]{0,20}search)\b", text, re.I)
        or re.search(r"\bnot\s+asking\s+(?:you\s+)?to\s+(?:start|run|perform)\s+(?:a\s+)?search\b", text, re.I)
    )


def manual_discovery_request(text: str) -> bool:
    return bool(search_forbidden(text) and re.search(r"\b(?:manual|google|gmail|discovery|lookup)\b", text, re.I))


def _positive_discovery_text(text: str) -> str:
    return ". ".join(re.split(r"\b(?:do not|don['’]?t|never)\b", clause, maxsplit=1, flags=re.I)[0]
                     for clause in re.split(r"[.!?;]\s*", text))


def _positive_audience(text: str) -> str | None:
    """Use an explicit role from this request, never a negated other role."""
    positive = _positive_discovery_text(text)
    roles = [
        (r"\b(?:house\s+)?cleaners?\b", "independent house cleaner"),
        (r"\bgym\s+owners?\b", "independent gym owner"),
        (r"\b(?:dog[- ]|pet[- ])?groomers?\b", "independent dog groomer"),
        (r"\b(?:home[- ]?)?childcare\s+providers?\b", "home childcare provider"),
        (r"\b(?:personal\s+)?trainers?\b", "independent personal trainer"),
    ]
    hits = {role for pattern, role in roles if re.search(pattern, positive, re.I)}
    return next(iter(hits)) if len(hits) == 1 else None


def discovery_query(text: str) -> str:
    """A bounded explicit-role lookup, not the surrounding chat instructions."""
    audience = _positive_audience(text)
    if not (audience and re.search(r"\b(?:find|lookup|look up|identify|locate|discover)\b", text, re.I)
            and not re.search(r"\b(?:competitors?|pricing|reviews?|alternatives?)\b", text, re.I)):
        return text.strip()[:600]
    positive = _positive_discovery_text(text)
    city = re.search(r"\b(?:in|near|around)\s+(Bengaluru|Bangalore|Mumbai|Delhi|Pune|Hyderabad|Chennai|Seattle|London|Austin|New York)\b", positive, re.I)
    return f'"{audience}"' + (f' "{city[1]}"' if city else "") + ' website contact'


def manual_discovery_reply(text: str, *, venture_id: str | None = None) -> dict:
    brief = {}
    if venture_id:
        from app.ventures.state_brief import get_venture_state_brief
        brief = get_venture_state_brief(venture_id) or {}
    idea = brief.get("idea") or {}
    explicit = _positive_audience(text)
    saved = _positive_audience(str(idea.get("audience") or ""))
    role = explicit or saved
    audience = role or str(idea.get("audience") or "potential customers")[:120]
    trainer = role == "independent personal trainer"
    city = "Bengaluru"
    positive = _positive_discovery_text(text)
    found = re.search(r"\b(?:in|near|around)\s+(Bengaluru|Bangalore|Mumbai|Delhi|Pune|Hyderabad|Chennai|Seattle|London|Austin|New York)\b", positive, re.I)
    if found:
        city = found[1]
    query = f'"{audience}" "{city}" website contact'
    link = "https://www.google.com/search?" + urlencode({"q": query})
    ask = ("how you generally reschedule a session, using anonymized verbal descriptions only" if trainer else
           "your general administrative workflow and one frustrating task, using an anonymized verbal description only")
    name = str(idea.get("name") or (brief.get("venture") or {}).get("name") or "a new idea")[:80]
    body = (f"Manual alternative: {city} is a suggested first test city, not a validated market. "
            "I have not searched, found any people, or contacted anyone in this reply.\n\n"
            f"1. Open Google ({link}). Search exactly:\n```text\n{query}\n```\n"
            "2. Open one relevant professional's own website. Read its location and services pages to check the fit. "
            "Check whether the solo/independent owner fit and requested business size are publicly established; otherwise record them as unknown. Never infer them from a directory label or request member/client data. "
            "Use only the contact email or contact form that person publishes for business enquiries. Do not guess an email, harvest lists or use a private address.\n"
            "3. Invite only a consenting adult about their own general workflow; do not observe children, pickups or parent conversations. Open Gmail, choose Compose, and enter that publicly listed business address yourself. Paste the draft below. "
            "Check the recipient, replace [Your name], and review the wording. Sending is your separate decision; nothing has been sent or scheduled. "
            "If only a contact form is available, review its purpose and paste the same request there instead.\n"
            "4. Bring the website URL and what its public pages actually show back to this chat. Record a reply or interview only after it happens; a candidate is not a user.\n\n"
            f"Review draft:\n```text\nSubject: A short question about your workflow\n\nHi,\n\n"
            f"I'm exploring {name} for {audience}. I’m learning about your workflow before making any product claims. "
            f"Would you be open to a 10-minute conversation about {ask}? "
            "Please do not share private client chats, contact lists, client addresses, child records, photos or family conversations. "
            "I'm looking to learn, with no sales pitch.\n\nThanks,\n[Your name]\n```")
    return {"kind": "text", "body_md": body, "citations": [], "actions": []}


def search_failure_reply(query: str, code: str = "") -> dict:
    reason = "Live search is temporarily rate-limited." if code == "search_rate_limited" else "Live search is unavailable right now."
    cleaned = discovery_query(query)
    reply = {"kind": "text", "citations": [], "actions": [
        {"label": "Open manual Google search", "href": "https://www.google.com/search?" + urlencode({"q": cleaned}), "style": "primary"}],
        "body_md": reason + " No live sources were verified by this search.\n\n"
        f"Open Google and search exactly:\n```text\n{cleaned}\n```\n"
        "Open a result's original website and check its location, role and public contact details. "
        "Bring the source URL and what it actually shows back here before choosing an outreach action. "
        "No person, email or outreach result has been invented; nothing was sent."}
    return reply
