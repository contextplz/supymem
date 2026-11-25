"""Server-side venture naming from a raw idea (29 §1.1).

A venture gets a short product name at create time — never the first clause
of the founder's sentence with an ellipsis.
"""

from __future__ import annotations

import re

_BAD = frozenset(
    {
        "",
        "untitled",
        "untitled venture",
        "new venture",
        "my venture",
        "test",
        "asdf",
        "xxx",
    }
)

_ID_RE = re.compile(
    r"^(ven|idea|chr|org|ag|qst|wo|ht|prop|obr|msg|doc|tsk)_",
    re.IGNORECASE,
)

_QUOTED = re.compile(r'[“"‘\']([^”"’\']{2,40})[”"’\']')

_LEAD = re.compile(
    r"^(?:"
    r"i['’]m\s+|we['’]re\s+"
    r"|i\s+was\s+thinking\s+of\s+(?:building\s+)?"
    r"|i(?:'d| would)\s+like\s+(?:to\s+)?"
    r"|i\s+want\s+(?:to\s+)?"
    r"|let'?s\s+"
    r"|we\s+(?:should|could|want to)\s+"
    r"|build(?:ing)?\s+"
    r"|create(?:ing)?\s+"
    r"|make(?:ing)?\s+"
    r")+",
    re.IGNORECASE,
)

_WRAPPER = re.compile(
    r"^(?:an?\s+|the\s+)?(?:"
    r"platform|marketplace|app|application|saas|tool|product|site|website|service"
    r")\s+(?:where|for|that|to|which)\s+",
    re.IGNORECASE,
)

_FILLER = re.compile(
    r"\b(?:kind of|sort of|basically|just|like|you know)\b",
    re.IGNORECASE,
)

_PRODUCT_LEAD = re.compile(
    r"^(?:(?:existing\s+)?product\s*:\s*|(?:i|we)\s+(?:already\s+)?(?:have|built|launched|run)\s+(?:an?\s+|our\s+|my\s+)?(?:existing\s+)?(?:(?:saas|software)\s+)?(?:app|product|mvp|saas|website)\s+(?:that|which|for|to)\s+|(?:it|this\s+(?:app|product))\s+)?(?:helps?\s+)?",
    re.IGNORECASE,
)

_STOP = frozenset(
    {
        "a",
        "an",
        "and",
        "are",
        "for",
        "from",
        "help",
        "helping",
        "helps",
        "in",
        "into",
        "lets",
        "it",
        "of",
        "on",
        "or",
        "our",
        "that",
        "the",
        "this",
        "to",
        "we",
        "where",
        "which",
        "who",
        "with",
        "you",
        "your",
    }
)

_GENERIC = frozenset(
    {
        "app",
        "application",
        "business",
        "company",
        "friends",
        "idea",
        "independent",
        "local",
        "new",
        "marketplace",
        "people",
        "platform",
        "product",
        "saas",
        "service",
        "simple",
        "site",
        "small",
        "software",
        "startup",
        "someone",
        "something",
        "system",
        "thing",
        "tool",
        "users",
        "venture",
        "website",
    }
)

# Greetings, pronouns, auxiliaries, quantifiers and common verbs: never the
# start (or end) of a public product name ("I Am", "User Can", "Hey Whatever",
# "Instagram's Most", "Museums Retain").
_NOISE = frozenset(
    {
        "i", "im", "ive", "me", "my", "mine", "we", "us", "you", "he", "she", "they", "them",
        "hey", "hi", "hello", "yo", "please", "whatever", "anything", "everything",
        "am", "is", "are", "was", "were", "be", "been", "can", "could", "will", "would",
        "should", "may", "might", "must", "do", "does", "did", "has", "have", "had",
        "user", "most", "more", "best", "top", "nearest", "better", "very", "really",
        "so", "not", "no", "yes", "if", "when", "while", "than", "then", "also", "all",
        "any", "some", "every", "each", "about", "at", "by", "as", "but", "what", "why",
        "how", "there", "here", "want", "need", "wanna", "gonna", "think", "idea",
        "reduce", "increase", "find", "track", "manage", "improve", "save", "stop",
        "upload", "share", "send", "buy", "sell", "learn", "connect", "discover", "automate", "generate",
        "allow", "allows", "enable", "enables", "provide", "provides", "offer", "offers",
    }
)

_PROFANE = frozenset(
    {"fuck", "shit", "bitch", "cunt", "dick", "cock", "pussy", "asshole", "bastard",
     "slut", "whore", "nigger", "fag", "retard", "porn", "sex", "nazi", "rape"}
)

_WEAK_VERB = frozenset(
    {
        "build",
        "create",
        "get",
        "give",
        "help",
        "join",
        "let",
        "make",
        "sing",
        "start",
        "take",
        "use",
    }
)


def _plain(token: str) -> str:
    return re.sub(r"'s?$", "", token.lower())


def is_valid_name(name: str | None) -> bool:
    """Public-page safety check: 1-3 brand-like words, not a sentence fragment."""
    n = (name or "").strip()
    words = n.split()
    if not 1 <= len(words) <= 3 or len(n) > 30 or n.lower() in _BAD or looks_like_id(n):
        return False
    if not all(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9&-]{0,19}", w) for w in words):
        return False
    low = [w.lower() for w in words]
    if set(low) <= _GENERIC:
        return False
    if low[0] in _NOISE or low[0] in _STOP or low[-1] in _NOISE or low[-1] in _STOP:
        return False
    if any(w in _PROFANE for w in low) or any(w.endswith(("help", "helps")) and len(w) > 5 for w in low):
        return False
    return not (len(words) > 1 and low[-1] in _WEAK_VERB)


def looks_like_id(s: str) -> bool:
    return bool(_ID_RE.match(s.strip()))


def _title_case(words: list[str]) -> str:
    out: list[str] = []
    for w in words:
        if not w:
            continue
        if out and w.lower() in {"and", "for", "of", "to"}:
            out.append(w.lower())
        else:
            out.append("AI" if w.lower() == "ai" else w[0].upper() + w[1:] if w[0].islower() else w)
    return " ".join(out)


def title_from_idea(idea: str | None) -> str:
    raw = re.sub(r"\s+", " ", (idea or "").strip())
    if not raw:
        return ""
    # Preserve a short founder-supplied category as a descriptive working
    # name. Joining its first two words invents awkward names such as
    # "AIsecurity" from "AI security monitoring". Longer briefs still use
    # the extraction below, and the founder can rename the venture later.
    short_phrase = re.fullmatch(r"[A-Za-z0-9' -]{2,48}", raw)
    ws = [w.lower() for w in raw.split()]
    if short_phrase and 2 <= len(ws) <= 4 and not (
        {ws[0], ws[-1]} & (_NOISE | _STOP | _WEAK_VERB | {"existing"}) or set(ws) & _PROFANE
    ):
        return _title_case(raw.split())[:48]
    quoted = _QUOTED.search(raw)
    if quoted:
        label = quoted.group(1).strip()
        if label:
            return _title_case(re.findall(r"[A-Za-z0-9']+", label))[:48]
    text = _LEAD.sub("", raw)
    text = _PRODUCT_LEAD.sub("", text)
    text = _WRAPPER.sub("", text)
    text = _PRODUCT_LEAD.sub("", text)
    text = _FILLER.sub(" ", text)
    text = re.sub(r"\s+", " ", text).strip(" .,;:—-")
    text = re.split(r"[.!?\n]", text, maxsplit=1)[0].strip()
    tokens = [re.sub(r"'s?$", "", t) for t in re.findall(r"[A-Za-z0-9']+", text)]
    content = [
        t
        for t in tokens
        if _plain(t) not in _STOP and _plain(t) not in _GENERIC and _plain(t) not in _NOISE
    ]
    if not content:
        content = [t for t in tokens if _plain(t) not in _STOP and _plain(t) not in _NOISE]
    if content and content[0].lower() in _WEAK_VERB and len(content) > 1:
        content = content[1:]
    nouns = [t for t in content if not t.lower().endswith("ing")]
    picked = nouns[:2] or content[:2]
    if not picked:
        return ""
    name = _title_case(picked)[:48]
    return name if is_valid_name(name) else (_title_case(picked[:1]) if is_valid_name(picked[0]) else "")


def needs_heal(name: str | None) -> bool:
    n = (name or "").strip()
    if len(n) < 2 or n.lower() in _BAD or looks_like_id(n):
        return True
    return n.endswith("…") or n.endswith("...")


VENTURE_NAME_MAX_CHARS = 48


def derive_name(name: str | None, idea: str) -> str:
    """Return a founder-facing company name; never junk, an ID, or a clipped sentence."""
    n = (name or "").strip()
    if n.endswith("…") or n.endswith("..."):
        n = ""
    if len(n) >= 2 and n.lower() not in _BAD and not looks_like_id(n):
        return n[:VENTURE_NAME_MAX_CHARS]
    from_idea = title_from_idea(idea)
    if from_idea:
        return from_idea
    if n and not looks_like_id(n) and len(n) >= 2:
        return n[:VENTURE_NAME_MAX_CHARS]
    return "Untitled venture"


def _numbered_name(name: str, number: int) -> str:
    suffix = f" {number}"
    return name[: 48 - len(suffix)] + suffix


def recipient_name(name: str, venture_id: str | None = None) -> str:
    from app.db.models import Venture, Workspace
    from app.state import db
    from app.tenancy import current_or_none
    from app.ventures.startup_review import _json

    scope = current_or_none()
    venture_id = venture_id or (scope.venture_id if scope else None)
    if not venture_id or not re.search(r" [1-9]\d*$", name):
        return name
    with db()() as session:
        venture = session.get(Venture, venture_id)
        workspace = session.get(Workspace, venture.workspace_id) if venture else None
        origin = _json(workspace.settings_json if workspace else None).get("venture_name_origin", {})
    if not isinstance(origin, dict):
        return name
    base = origin.get("base")
    number = int(name.rsplit(" ", 1)[1])
    if isinstance(base, str) and len(base) <= 48 and origin.get("name") == name and number >= 2 and _numbered_name(base, number) == name:
        return base
    # shortcut: older names without provenance stay intact; recover only with a saved original name.
    return name


def unique_venture_name(owner_id: str, name: str) -> str:
    """Append " 2", " 3"... so a founder's live ventures never share a name."""
    from sqlalchemy import select

    from app.db.models import Venture, Workspace
    from app.state import db

    with db()() as session:
        taken = {n.lower() for n in session.scalars(
            select(Venture.name).join(Workspace, Workspace.id == Venture.workspace_id)
            .where(Workspace.owner_id == owner_id, Venture.archived_at.is_(None)))}
    candidate, i = name, 1
    while candidate.lower() in taken:
        i += 1
        candidate = _numbered_name(name, i)
    return candidate


def slug_for_email(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "", name.lower())
    return (s[:24] or "venture")


_NAME_SYSTEM = (
    "You name products. Given a founder's idea, reply with ONLY a short, pronounceable, "
    "brandable product name: 1 to 3 words, no sentence fragments, no pronouns or greetings, "
    "nothing silly or crude. Real-sounding names like 'KilnScout' or 'ShelfSprout'. No quotes, no explanation."
)


def clean_llm_name(text: str | None) -> str | None:
    first = (text or "").strip().splitlines()[0] if (text or "").strip() else ""
    name = re.sub(r"\s+", " ", first.strip(" \t\"'`*.,;:!-")).strip()
    return name if is_valid_name(name) else None


async def suggest_name(idea: str, workspace_id: str) -> str | None:
    """Cheap flash-tier brand name (credit-metered via the chat helper); None on any failure."""
    import asyncio

    if not (idea or "").strip():
        return None
    try:
        from app.chat.assistant import _complete

        text = await asyncio.wait_for(
            _complete(
                [{"role": "system", "content": _NAME_SYSTEM}, {"role": "user", "content": idea.strip()[:1500]}],
                workspace_id=workspace_id,
            ),
            timeout=10.0,
        )
    except Exception:  # noqa: BLE001
        return None
    return clean_llm_name(text)
