"""Claims guard for anything God mode makes public or sends: no invented proof, no features that do not exist.

Deterministic, like firstlook/lint.py (which this runs on top of for page copy). Autopilot treats every
page as pre-product: a present-tense claim that the product already does something is a violation unless
the sentence is hedged (waitlist, early access, will, soon). A founder who publishes by hand is not bound by it.
"""

from __future__ import annotations

import re
from typing import Any

HONESTY_RULES = ("Describe only what exists today. This is early access or a waitlist unless the founder says the product "
                 "is live: no claim that the product already does something, no customer counts, metrics, testimonials, "
                 "awards or guarantees. Never invent conversations, interviews, customers, traction, research, or personal "
                 "experience. Only repeat such claims when the founder explicitly stated them in saved facts or their own "
                 "words. An earlier generated draft and a recipient's public page are not evidence of the founder's experience.")
_PROOF = re.compile(
    r"\b\d[\d,.]*\s?(?:\+|k\b)?\s+(?:happy |active |paying )?(?:customers|users|clients|teams|businesses|gyms|companies|owners|members)\b"
    r"|trusted by|as seen (?:in|on)|testimonial|award[- ]winning|\b#1\b|best[- ]in[- ]class|\bguarantee[ds]?\b"
    r"|\b\d+\s?% (?:more|less|faster|increase|reduction|savings?|growth|lift)\b", re.I)
_LIVE = re.compile(
    r"\b(?:we|our (?:app|platform|product|software|tool|service))\s+(?:already |now |currently |automatically )?"
    r"(?:send|sends|automate|automates|track|tracks|handle|handles|manage|manages|integrate|integrates|connect|connects|"
    r"generate|generates|detect|detects|remind|reminds|save|saves)\b", re.I)
_HEDGE = re.compile(r"\b(?:will|soon|planned|planning|building|early access|waitlist|coming|working on|aim to|hope to|would|could)\b", re.I)
# shortcut: explicit English firsthand claims, use a grounded semantic check before supporting other languages.
_EXPERIENCE = re.compile(
    r"\b(?:i|we)(?:['’](?:ve|m|re))?\s+(?:(?:have|am|are|been|keep|kept|already|often|recently|regularly|personally)\s+){0,4}"
    r"(?:hear(?:d|ing)?|spoke(?:n)?|talk(?:ed|ing)?|interview(?:ed|ing)?|learn(?:ed|t)?|discover(?:ed)?|found|"
    r"research(?:ed)?|survey(?:ed)?|help(?:ed)?|work(?:ed|ing)?\s+with|(?:have|had)\s+\w+(?:\s+\w+){0,4}\s+(?:using|paying)|"
    r"(?:have|had|serve|served|support|supported)\s+(?:\w+\s+){0,3}(?:customers?|users?|clients?|subscribers?|pilots?))\b"
    r"|\b(?:my|our)\s+(?:experience|research|findings|interviews|customers?|users?|clients?)\b"
    r"|\b(?:customers?|users?|clients?|people)\s+(?:tell|told|say|said)\s+(?:me|us)\b"
    r"|\b(?:research|studies|interviews|feedback)\s+(?:shows?|suggests?|confirms?|found|reveals?)\b", re.IGNORECASE)


def generated_email(draft: dict) -> bool:
    return draft.get("copy_origin", draft.get("source")) in {"model", "llm", "template", "chat", "opengtm", "source_quotes"}


def founder_words(venture_id: str) -> str:
    from sqlalchemy import select

    from app.autopilot.outreach import KIND
    from app.chat.store import list_messages
    from app.db.models import Venture, VentureDocument
    from app.gtm.journey import _json
    from app.secrets.detect import looks_like_secret
    from app.state import db
    from app.tenancy import current_or_none
    from app.ventures import founder_facts

    with db()() as session:
        venture = session.get(Venture, venture_id)
        if not venture:
            return ""
        words = [venture.raw_idea or ""]
        for fact in founder_facts.read(session, venture).values():
            if fact.get("source") in founder_facts.FOUNDER_SOURCES:
                words.extend(str(fact.get(key) or "") for key in ("value", "quote"))
        for document in session.scalars(select(VentureDocument).where(VentureDocument.venture_id == venture_id, VentureDocument.kind == KIND)):
            metadata = _json(document.provenance_json)
            if metadata.get("copy_origin") == "founder":
                words.append(document.body_md or "")
            if isinstance(metadata.get("founder_copy"), str):
                words.append(metadata["founder_copy"])
    # shortcut: only the latest 200 messages, persist older statements as founder facts when histories exceed this limit.
    try:
        messages = list_messages(venture_id, limit=200)
    except ValueError as error:
        scope = current_or_none()
        if (str(error) != "chat_conversation_not_found" or scope is None
                or scope.workspace_id != venture.workspace_id or scope.venture_id == venture_id):
            raise
        # shortcut: inaccessible chat uses saved facts only, persist missing statements as founder facts when cross-conversation proof is needed.
        messages = []
    words.extend(message.get("body_md") or "" for message in messages
                 if message.get("role") in {"user", "founder"})
    return "\n".join(text for text in words if not looks_like_secret(text))


def _supported(sentence: str, words: str) -> bool:
    def tokens(text):
        text = re.sub(r"['’](?:ve|m|re)\b", " ", text.casefold())
        return set(re.findall(r"[a-z0-9]+", text)) - {"i", "we", "have", "am", "are", "my", "our", "a", "an", "the"}

    wanted = tokens(sentence)
    return bool(wanted) and any(wanted <= tokens(fact) and not re.match(
        r"\s*(?:please|draft|write|rewrite|make|change|say|remove|avoid|delete|shorten)\b", fact, re.IGNORECASE) and not re.search(
        r"\b(?:not|never|no|haven['’]t|didn['’]t|don['’]t|hypothetical|imagine)\b", fact, re.IGNORECASE)
        for fact in re.split(r"(?<=[.!?])\s+|\n+", words))


def founder_proof(venture_id: str) -> str:
    return "\n".join(sentence for sentence in re.split(r"(?<=[.!?])\s+|\n+", founder_words(venture_id))
                     if (_EXPERIENCE.search(sentence) or _PROOF.search(sentence) or _LIVE.search(sentence))
                     and _supported(sentence, sentence))


def violations(text: str, *, product_live: bool = False, founder_words: str | None = None) -> list[str]:
    """Reasons this text cannot go out as is; empty when it is honest."""
    out: list[str] = []
    for sentence in re.split(r"(?<=[.!?])\s+|\n+", text or ""):
        if founder_words is not None and _supported(sentence, founder_words):
            continue
        if _PROOF.search(sentence):
            out.append("unverifiable_proof: " + sentence.strip()[:80])
        elif founder_words is not None and any(not re.fullmatch(
                r"\s*(?:can|could|would|may|shall|will|have|did|do|are)\s+", sentence[:match.start()], re.IGNORECASE)
                for match in _EXPERIENCE.finditer(sentence)):
            out.append("unsupported_experience: " + sentence.strip()[:80])
        elif not product_live and _LIVE.search(sentence) and not _HEDGE.search(sentence):
            out.append("claims_unbuilt_feature: " + sentence.strip()[:80])
    return out


def site_violations(venture_id: str) -> list[str]:
    """The saved landing copy (what a publish would show) against the lint and this guard."""
    from app.firstlook import versions
    from app.firstlook.lint import lint_copy
    from app.firstlook.service import _workspace_and_idea

    wid, _, _ = _workspace_and_idea(venture_id)
    latest = versions.get_latest(wid) if wid else None
    if latest is None:
        return []
    copy: dict[str, Any] = latest.copy
    text = " ".join([str(copy.get("headline") or ""), str(copy.get("subhead") or ""),
                     *(f"{p.get('title', '')}. {p.get('body', '')}" for p in copy.get("points") or [] if isinstance(p, dict))])
    return violations(text, founder_words=founder_words(venture_id)) + [f"lint: {r}" for r in lint_copy(copy).reasons]
