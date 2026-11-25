"""Conservative admission of search leads to idea-specific research evidence.

Search rank and a matching query do not establish that a returned page is about
the founder's problem. This lexical gate only rejects obvious mismatches; an
admitted snippet remains an unverified lead, never market proof.
"""
from __future__ import annotations

import re
from collections import Counter
from urllib.parse import urlsplit

_WORDS = re.compile(r"[a-z0-9]{3,}")
_GENERIC = {
    "about", "across", "after", "against", "also", "and", "app", "apps", "are",
    "business", "businesses", "can", "customer", "customers", "existing", "for",
    "from", "founder", "founders", "idea", "into", "market", "new", "our",
    "platform", "product", "products", "saas", "service", "services", "software",
    "startup", "startups", "team", "teams", "that", "the", "their", "them",
    "these", "this", "tool", "tools", "using", "users", "with", "workflow",
}
# Founders and vendors name the same idea differently ("cancel" vs "churn",
# "gym" vs "fitness club"). Fold a few unambiguous synonyms to one term on both
# sides so a real competitor is not rejected for wording alone.
_SYNONYMS = {
    "cancel": "churn", "cancellation": "churn", "canceling": "churn", "cancelling": "churn",
    "attrition": "churn", "retention": "churn", "retain": "churn",
    "fitness": "gym", "club": "gym", "studio": "gym",
    "membership": "member", "prediction": "predict", "predictive": "predict",
    "forecast": "predict", "likely": "predict",
}
_LEGAL_PATH = re.compile(r"(?:^|/)(?:privacy|privacy-policy|terms|terms-of-service|"
                         r"terms-and-conditions|legal|cookie-policy|cookies)(?:/|\.|$)", re.I)
_LEGAL_TITLE = re.compile(r"\b(?:privacy policy|terms of (?:service|use)|cookie policy)\b", re.I)
_ROUNDUP_TITLE = re.compile(r"\b(?:best|top\s+\d+|roundup|list of|tools? to try)\b", re.I)
_NAME_SOURCE = re.compile(r"\b(?:best|top\s+\d+|roundup|list of|alternatives?|compare|comparison|vs\.?|versus)\b", re.I)
_CUSTOMER_VOICE = re.compile(r"\b(?:i|we)\s+(?:need|want|wish|struggle|cannot|can't)\b", re.I)


def name_source(source: dict) -> bool:
    """Roundups discover names only; they never become market evidence."""
    return bool(_NAME_SOURCE.search(str(source.get("title") or "")))


def relevant_company_lead(plan: dict, source: dict) -> bool:
    """Match the fetched description to category/job terms, never the query."""
    title = str(source.get("title") or "")
    text = title + " " + str(source.get("snippet") or "")
    try:
        path = urlsplit(str(source.get("url") or "")).path
    except ValueError:
        return False
    if _LEGAL_PATH.search(path) or _LEGAL_TITLE.search(title) or name_source(source):
        return False
    terms = " ".join([*plan.get("category_terms", []), *plan.get("job_phrases", [])])
    overlap = _stems(terms) & _stems(text)
    words = " " + " ".join(_word_list(text, category_phrase=True)) + " "
    excluded = [" ".join(_word_list(term, category_phrase=True)) for term in plan.get("negative_terms", [])]
    if not overlap or any(" " + term + " " in words for term in excluded if term):
        return False
    return len(overlap) >= min(2, len(_stems(terms)))


def _word_list(text: str, *, category_phrase: bool = False) -> list[str]:
    # Simple singularization covers routine plural search variants without
    # depending on a language model or allowing unrelated synonym guesses.
    words = []
    for word in _WORDS.findall(text.lower()):
        ignored = (_PRODUCT_WORD.fullmatch(word) or _PRODUCT_WORD.fullmatch(word.rstrip("s"))) if category_phrase else word in _GENERIC
        if ignored:
            continue
        if word.endswith("ies") and len(word) > 5:
            word = word[:-3] + "y"
        elif category_phrase and word.endswith("sses"):
            word = word[:-2]
        elif word.endswith("s") and len(word) > 4 and (not category_phrase or not word.endswith("ss")):
            word = word[:-1]
        words.append(_SYNONYMS.get(word, word))
    return words


def _tokens(text: str) -> set[str]:
    return set(_word_list(text))


def _nearby_pairs(text: str, distance: int) -> set[frozenset[str]]:
    words = _word_list(text)
    return {
        frozenset((word, other))
        for index, word in enumerate(words)
        for other in words[index + 1:index + distance + 1]
        if word != other
    }


class Topic(str):
    """The research topic text; `.customer` is the founder's customer phrase, used to recognise category competitors."""
    customer = ""


def research_topic(snapshot: dict) -> str:
    """Use the founder's problem before broader customer or offer prose."""
    for key in ("problem", "offer", "summary", "customer"):
        value = str(snapshot.get(key) or "").strip()
        if value:
            topic = Topic(value)
            topic.customer = str(snapshot.get("customer") or "")
            return topic
    site = snapshot.get("existing_site")
    return str(site.get("url") or "") if isinstance(site, dict) else ""


_PRODUCT_WORD = re.compile(r"\b(?:software|tool|app|platform|saas|system)\b", re.I)


def _stems(text: str) -> set[str]:
    return {word[:5] for word in _word_list(text)}


def _category_product(topic: str, title: str, parsed, snippet: str) -> bool:
    """A product page that names the founder's customer and touches the problem.

    Vendors of a real competitor describe the customer and category ("coffee roasting software"),
    not the founder's own problem wording. Two customer terms in the title/host/path, a product word,
    and one problem term anywhere keep out generic "small business software" pages.
    ponytail: 5-letter prefix stems; a real stemmer if recall is still weak.
    """
    customer = _stems(getattr(topic, "customer", ""))
    if len(customer) < 2 or not _PRODUCT_WORD.search(title + " " + snippet):
        return False
    strong = _stems(" ".join((title, parsed.hostname or "", parsed.path)))
    return len(customer & strong) >= 2 and bool(_stems(topic) & (strong | _stems(snippet)))


def relevant_search_lead(topic: str, source: dict) -> bool:
    """Keep topical leads and reject legal pages and broad-query drift.

    Search-query terms are never counted as source evidence. A broad mention in
    a snippet is insufficient: the title or URL must name two related terms
    from the problem near each other, unless a customer discussion itself
    states the problem in first person.
    """
    title = str(source.get("title") or "")
    url = str(source.get("url") or "")
    snippet = str(source.get("snippet") or "")
    try:
        parsed = urlsplit(url)
    except ValueError:
        return False
    if _LEGAL_PATH.search(parsed.path) or _LEGAL_TITLE.search(title):
        return False
    # A category roundup can mention many of the founder's words without
    # describing a specific competing product or a customer's problem. Keep
    # it out of the evidence pool even when lexical overlap is high.
    if _ROUNDUP_TITLE.search(title):
        return False
    customer = getattr(topic, "customer", "")
    if customer:
        # Persona overlap is not evidence of the problem. Inspect the source's
        # own text for distinct problem facets before any category shortcut.
        # Generic quality adjectives are not a distinct job facet by themselves.
        # One actual facet still permits short category descriptions such as
        # bakery order software; no extra lexical-match quota is introduced.
        descriptive = _stems("quick reliable easy simple fast slow better good bad")
        facets = _stems(topic) - _stems(customer) - descriptive
        source_facets = _stems(" ".join((title, parsed.hostname or "", parsed.path, snippet)))
        if facets and not facets & source_facets:
            return False
    if _category_product(topic, title, parsed, snippet):
        return True
    topic_sequence = _word_list(topic)
    topic_counts = Counter(topic_sequence)
    topic_words = set(topic_counts)
    if not topic_words:
        return bool(title or snippet)
    topic_pairs = _nearby_pairs(topic, 4)
    source_pairs = (_nearby_pairs(title, 3) | _nearby_pairs(parsed.hostname or "", 3)
                    | _nearby_pairs(parsed.path, 3))
    focused_pair = bool(topic_pairs & source_pairs)
    strong_words = _tokens(title) | _tokens(parsed.hostname or "") | _tokens(parsed.path)
    strong_matches = topic_words & strong_words
    snippet_matches = topic_words & _tokens(snippet)
    all_matches = strong_matches | snippet_matches
    # Repeated category words in a founder's longer description (for example
    # "agent security") are weak by themselves. Require a distinct problem
    # facet as well (for example access, permissions, or audit).
    repeated = {word for word, count in topic_counts.items() if count > 1}
    facets = topic_words - repeated
    if repeated and facets and (not all_matches & repeated or not all_matches & facets):
        return False
    if len(topic_words) == 1:
        return bool(all_matches)
    host = (parsed.hostname or "").lower()
    discussion = (host.startswith(("forum.", "community.")) or host.endswith(".reddit.com")
                  or host == "reddit.com")
    customer_discussion = (discussion and bool(_CUSTOMER_VOICE.search(snippet))
                           and len(snippet_matches) >= 3 and bool(snippet_matches & facets))
    if len(all_matches) < min(3, len(topic_words)):
        # Repository names are often short, while their metadata description
        # supplies the second topic term. Keep this explicit GitHub evidence
        # without admitting an ordinary page on the same weak pattern.
        if source.get("repository") and strong_matches and len(all_matches) >= 2:
            return True
        return focused_pair
    if focused_pair:
        return True
    return customer_discussion


def plausible_review_lead(topic: str, source: dict) -> bool:
    """Weak topical screen for explicitly unverified search results.

    This never qualifies a result as evidence. It only keeps a small set of
    pages worth opening when the stricter evidence gate rejects every hit.
    """
    title = str(source.get("title") or "")
    snippet = str(source.get("snippet") or "")
    try:
        parsed = urlsplit(str(source.get("url") or ""))
    except ValueError:
        return False
    if _LEGAL_PATH.search(parsed.path) or _LEGAL_TITLE.search(title) or _ROUNDUP_TITLE.search(title):
        return False
    topic_words = _tokens(topic)
    if len(topic_words) < 2:
        return False
    strong = _tokens(title) | _tokens(parsed.hostname or "") | _tokens(parsed.path)
    all_matches = topic_words & (strong | _tokens(snippet))
    return len(all_matches) >= 2 and bool(topic_words & strong)
