"""Fail-closed, own-site qualification and grounded need evidence."""
from __future__ import annotations

import bisect
import hashlib
import logging
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

MIN_RELEVANCE = 0.7
POSSIBLE_FIT_RELEVANCE = 0.6
MAX_POSSIBLE_FITS = 5
EXCLUDE_TAGS = ["nav", "header", "footer", "aside"]


class PassageScore(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    id: int = Field(ge=0)
    relevance: float = Field(ge=0, le=1)


class MarketRole(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    role_in_market: Literal["buyer", "vendor/competitor", "directory/listing", "media", "consumer/individual", "unknown"]
    icp_fit: float = Field(ge=0, le=1)
    anti_icp: bool
    homepage_quote: str = Field(min_length=12, max_length=1000)


class PassageScores(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    items: list[PassageScore] = Field(max_length=80)


def blocked_page(text: str) -> bool:
    from app.gtm.enrich import _clean
    text = " ".join(_clean(text)[0].split()).strip(" #*_").casefold()
    # shortcut: match leading English interstitial cues, add markup or languages from verified block-page fixtures.
    if re.match(r"(?:whoa there,? pardner|(?:your request has been )?blocked due to a network policy|checking your browser|"
            r"enable javascript and cookies to continue|please verify you are a human)\b", text):
        return True
    return len(text) <= 1200 and bool(re.match(
        r"(?:just a moment(?:\.{3}|…|[.!]|$)|attention required\b|access denied\b|authentication required\b|"
        r"(?:log in|sign in) to continue\b)", text) or re.match(
        r"(?:http(?:/[\d.]+)?\s+|(?:http\s+)?(?:error|status(?: code)?)\s*[: -]?\s*)?(?:401|403|429)"
        r"(?:\s*[-:]?\s*(?:unauthorized|forbidden|too many requests|access denied|authentication required)\b|[.!]?$)", text))


def main_text(raw: str, *, consumer_thread: bool = False) -> str:
    from app.gtm.enrich import _clean
    if blocked_page(raw):
        return ""
    if re.search(r"<[a-z][^>]*>", raw, re.I):
        from lxml import etree, html
        try:
            document = html.fromstring(raw)
            for node in document.xpath("//nav|//header|//footer|//aside|//script|//style|//svg|//head|//template|//noscript|//*[@hidden or @aria-hidden='true' or @role='navigation' or @role='banner' or @role='contentinfo']"):
                if node is document:
                    return ""
                node.drop_tree()
            if consumer_thread and (main := document.xpath("//main|//*[@role='main']")):
                document = main[0]
            raw = html.tostring(document, encoding="unicode")
            if consumer_thread:
                raw = re.sub(r'<a\b[^>]*href=["\']([^"\']+)["\'][^>]*>([\s\S]*?)</a>',
                    lambda match: f"[{_clean(match[2])[0]}]({match[1]})", raw, flags=re.I)
            raw = re.sub(r"</?(?:a|span|strong|em|b|i|small|u)\b[^>]*>", "", raw, flags=re.I)
        except (etree.ParserError, ValueError):
            return ""
    text = _clean(raw)[0]
    lines, seen = [], set()
    for line in text.splitlines():
        line = " ".join(line.split())
        if not line or line.casefold() in seen:
            continue
        if consumer_thread and re.fullmatch(r"Reddit\s*[-–—]\s*The heart of the internet|(?:accept|reject)(?: all)? cookies|manage cookies", line, re.I):
            continue
        links = re.findall(r"\[[^\]]*\]\([^)]*\)", line)
        if (re.search(r"skip to (?:main )?content|all rights reserved|privacy policy|cookie (?:policy|settings|consent)|terms (?:and|&) conditions", line, re.I)
                or sum(map(len, links)) > len(line) / 2 and not (consumer_thread and re.match(r"(?:author|posted by|submitted by|by)\b", line, re.I))
                or re.fullmatch(r"home|menu|drinks|specials|about|contact(?: us)?|book (?:a )?table|sign (?:in|up)|log in", line, re.I)):
            continue
        seen.add(line.casefold())
        lines.append(line)
    return "\n".join(lines)


def readable_thread(text: str, metadata: dict) -> bool:
    return metadata.get("statusCode", 200) < 400 and not blocked_page(text) and len(main_text(text, consumer_thread=True).strip()) >= 400


def signal_pages(hit: dict, text: str, context: dict) -> list[dict]:
    from app.gtm.journey import _consumer_thread_url, _person_audience
    if hit.get("signal_pages") and _person_audience(context) and _consumer_thread_url(hit["url"]):
        return hit["signal_pages"]
    pages = [{"url": hit["url"], "text": text}]
    if _person_audience(context) and _consumer_thread_url(hit["url"]):
        snippet = str(hit.get("snippet") or hit.get("description") or "")
        if snippet.strip():
            pages.insert(0, {"url": hit["url"], "text": snippet, "source": "search_snippet"})
    return pages


def passages(pages: list[dict], context: dict | None = None) -> list[dict]:
    result = []
    for page in pages:
        text = main_text(page["text"])
        for line in text.splitlines():
            for match in re.finditer(r"[^\n]+?(?:[.!?](?=\s|$)|$)", line):
                quote = match[0].strip()
                if len(quote) >= 12:
                    result.append({"id": len(result), "url": page["url"], "text": quote,
                        **({"author": page["author"]} if page.get("author") else {}),
                        **({"source": page["source"]} if page.get("source") else {})})
    if len(result) > 80:
        from app.gtm.journey import _keywords, _person_audience
        from app.research.source_quality import _stems
        context = context or {}
        targeting = " ".join(str(context.get(key) or "") for key in ("audience", "problem"))
        terms = _stems(_keywords(targeting, len(targeting.split())))
        def rank(passage: dict) -> tuple[bool, float]:
            quote = passage["text"]
            score = len(terms & _stems(quote))
            if (_person_audience(context) and re.search(r"\b(?:i|my|we|our)\b", quote, re.I)
                    and re.search(r"\b(?:needs?|struggl\w*|can['’]t|exhausted|help|don['’]t know)\b", quote, re.I)):
                score += 0.5
            return passage.get("source") == "search_snippet", score
        # ponytail: lexical recall within 80 scored sentences; chunk if relevant passages still overflow.
        result = sorted(sorted(result, key=rank, reverse=True)[:80], key=lambda passage: passage["id"])
        for index, passage in enumerate(result):
            passage["id"] = index
    return result


def grounded_quote(quote: str, text: str) -> str | None:
    normalized = " ".join(quote.split())
    if not normalized:
        return None
    match = re.search(r"\s+".join(re.escape(part) for part in normalized.split()), text)
    return match[0] if match else None


def homepage_quote(quote: str, text: str) -> str | None:
    """The page span behind a model's homepage quote, tolerating what the model saw differently.

    The model reads judge_context text (HTML-escaped, NFKC, PII as [REDACTED]) and drops markdown
    (**bold**, [label](url), # headings) and curly quotes when quoting; same words in the same order
    still ground it. The returned span is verbatim page text.
    """
    import html
    import unicodedata
    exact = grounded_quote(quote, text)
    if exact:
        return exact
    word = r"[^\W_]+"
    fold = lambda value: unicodedata.normalize("NFKC", value).casefold()
    segments = [re.findall(word, fold(part)) for part in re.split(r"\[redacted\]", fold(html.unescape(quote)))]
    segments = [segment for segment in segments if segment]
    if sum(map(len, segments)) < 3:
        return None
    tokens = list(re.finditer(word, re.sub(r"\]\([^)\s]*\)", lambda link: " " * len(link[0]), text)))
    starts, joined = [], ""
    for token in tokens:
        starts.append(len(joined))
        joined += fold(token[0]) + " "
    # A redacted email or phone becomes a few page words.
    pattern = r" (?:\S+ ){0,8}".join(re.escape(" ".join(segment)) for segment in segments)
    match = re.search(r"(?<!\S)" + pattern + r"(?!\S)", joined)
    if not match:
        return None
    first, last = starts.index(match.start()), bisect.bisect_right(starts, match.end() - 1) - 1
    return text[tokens[first].start():tokens[last].end()]


log = logging.getLogger(__name__)


async def classify_home(home: str, text: str, context: dict, payload: dict) -> dict | None:
    from app.gtm import email_learning, enrich, journey
    from app.core.jobs import FatalError, RetryableError
    from app.gtm.reachable import trace
    if blocked_page(text):
        trace(payload, home, "rejected", "blocked_page")
        return None
    text = main_text(text)[:16000]
    if not text or enrich.classify(home) != "company":
        trace(payload, home, "rejected", "not_company_page")
        return None
    from app.gtm.reachable import _CHAINS
    if re.search(r"\bindependent\b|\b1 to 3 dentists\b", context.get("audience") or "", re.I) and _CHAINS.search(text):
        trace(payload, home, "rejected", "chain_not_independent")
        return None
    if journey._same_offer(home, "", text, context.get("competitors") or [], context.get("offer") or "", context.get("problem") or "",
            audience=context.get("audience") or ""):
        trace(payload, home, "rejected", "same_offer")
        return None
    targeting = {key: context.get(key, "") for key in ("audience", "problem", "offer", "competitors", "mode", "founder_note")}
    targeting["target_type"] = "person" if journey._person_audience(context) else "business"
    identity = [payload["workspace_id"], enrich._host(home), targeting, hashlib.sha256(text.encode()).hexdigest()]
    key = email_learning.cache_key("gtm", "marketrole-v3", identity)
    saved = email_learning.cached(key)
    try:
        response = saved if saved is not None else await journey._discovery_model(payload, "marketrole", targeting | {
            "_domain": enrich._host(home), "candidates": [{"id": 0, "url": home, "text": text}]})
        verdict = MarketRole.model_validate(response).model_dump()
    except FatalError as error:
        if str(error) not in {"discovery_outcome_uncertain", "discovery_provider_rejected", "discovery_provider_offline",
                "discovery_credentials_unavailable", "discovery_boundary_blocked"}:
            raise
        trace(payload, home, "rejected", "marketrole_" + str(error))
        return None
    except RetryableError:
        raise
    except Exception as exc:
        trace(payload, home, "rejected", "marketrole_failed_" + re.sub(r"[^a-z]", "", type(exc).__name__.lower())[:30])
        log.warning("gtm_marketrole_failed", extra={"exception_type": type(exc).__name__})
        return None
    quote = homepage_quote(verdict["homepage_quote"], text)
    if not quote:
        trace(payload, home, "rejected", "quote_ungrounded")
        return None
    if saved is None:
        email_learning.cache_save(key, verdict)
    if (verdict["anti_icp"] or verdict["icp_fit"] < MIN_RELEVANCE
            or verdict["role_in_market"] not in ({"buyer", "consumer/individual"} if context.get("mode") == "B2C" else {"buyer"})):
        trace(payload, home, "rejected", "anti_icp" if verdict["anti_icp"] else "low_fit" if verdict["icp_fit"] < MIN_RELEVANCE else "role_" + re.sub(r"\W+", "_", str(verdict["role_in_market"]))[:20])
        return None
    verdict["homepage_quote"] = quote
    return verdict


async def select_passages(pages: list[dict], context: dict, payload: dict, *, identity: str, diagnostics: dict | None = None) -> list[dict]:
    from app.gtm import journey
    from app.core.jobs import FatalError, RetryableError
    from app.gtm.reachable import trace
    for page in pages:
        if blocked_page(page["text"]):
            trace(payload, page["url"], "dropped", "blocked_page")
    choices = passages(pages, context)
    if diagnostics is not None:
        diagnostics.update(passage_count=len(choices), scored_count=0, max_relevance=0.0, threshold=MIN_RELEVANCE)
    if not choices:
        return []
    try:
        response = await journey._discovery_model(payload, "passages", {key: context.get(key, "") for key in ("audience", "problem", "mode")} | {
            "_domain": identity, "need_query": f"{context.get('audience', '')} needs or struggles with {context.get('problem', '')}",
            "candidates": choices})
        scores = PassageScores.model_validate(response).items
        if diagnostics is not None:
            diagnostics.update(scored_count=len(scores), max_relevance=max((item.relevance for item in scores), default=0.0))
    except FatalError as error:
        if str(error) not in {"discovery_outcome_uncertain", "discovery_provider_rejected", "discovery_provider_offline",
                "discovery_credentials_unavailable", "discovery_boundary_blocked"}:
            raise
        return []
    except RetryableError:
        raise
    except Exception:
        return []
    ids = [item.id for item in scores]
    if len(ids) != len(set(ids)) or set(ids) != {choice["id"] for choice in choices}:
        return []
    ranked = sorted(scores, key=lambda item: item.relevance, reverse=True)
    possible = context.get("mode") == "B2C" and ranked[0].relevance < MIN_RELEVANCE
    threshold = POSSIBLE_FIT_RELEVANCE if possible else MIN_RELEVANCE
    return [{key: value for key, value in choices[item.id].items() if key != "id"} | {"relevance": item.relevance}
            | ({"evidence_label": "possible_fit"} if possible else {})
            for item in ranked if item.relevance >= threshold]


def limit_fits(items: list[dict], strong_limit: int) -> list[dict]:
    return ([item for item in items if item.get("evidence_label") != "possible_fit"][:strong_limit]
        + [item for item in items if item.get("evidence_label") == "possible_fit"][:MAX_POSSIBLE_FITS])


def fit_passages(pages: list[dict], context: dict, role: dict | None, routes: list[dict], *, identity: str) -> list[dict]:
    from app.gtm import enrich
    role = role or {}
    if (context.get("mode") != "B2B" or role.get("role_in_market") != "buyer"
            or role.get("anti_icp") is not False or role.get("icp_fit", 0) < MIN_RELEVANCE):
        return []
    host = enrich._host(identity)
    if not any((route["kind"] in {"email", "booking", "contact_form", "phone", "website"}
            or route["kind"] in {"linkedin", "x"} and route.get("person"))
            and route.get("route_type", "published") == "published" and enrich._host(route["source_url"]) == host for route in routes):
        return []
    return [{"url": page["url"], "text": quote, "relevance": 0.0} for page in pages
        if enrich._host(page["url"]) == host and (quote := grounded_quote(role.get("homepage_quote", ""), page["text"]))][:1]


def dedupe(leads: list[dict]) -> list[dict]:
    from urllib.parse import urlsplit
    from app.gtm import enrich, reachable
    from app.core.local_mode import is_local
    seen, result = set(), []
    for lead in leads:
        author_route = next((route.get("url") for route in lead.get("routes", []) if route.get("profile")), None)
        url = lead.get("author_url") or author_route or lead.get("profile_url") or lead.get("website") or lead.get("source_url") or ""
        profile = reachable.profile_key(url)
        host = enrich._host(url)
        personal = lead.get("kind") == "person" or host in {"linkedin.com", "x.com", "twitter.com", "reddit.com"} or bool(lead.get("handle") or lead.get("author") or author_route)
        name = " ".join(str(lead.get("person") or (lead.get("name") if lead.get("kind") == "person" else "") or "").casefold().split())
        key = ("person", profile) if personal else ("domain", enrich._host(lead.get("business_url") or url))
        if is_local() and (lead.get("business_url") or host and lead.get("company") and not author_route
                and not lead.get("author") and host not in {"linkedin.com", "x.com", "twitter.com", "reddit.com"}
                and re.search(r"/(?:team|people|leadership|staff)(?:/|$)", urlsplit(url).path, re.I)
                and enrich.classify(url) == "company"):
            # shortcut: one published domain per organisation, use verified organisation IDs for companies spanning domains.
            domain = enrich._host(lead.get("business_url") or url)
            key = ("named_at_domain", domain, name) if personal and name and lead.get("role_match") else ("domain", domain)
        if not key[1]:
            key = ("person", str(lead.get("email") or lead.get("person") or lead.get("name") or "").casefold())
        if not key[1] or key in seen:
            continue
        # One person on two pages of an employer's site is one person; callers sort best first, so the better source stays.
        source = enrich._host(lead.get("source_url") or url)
        same_person = {("named", name, employer) for employer in (
            " ".join(str(lead.get("company") or "").casefold().split()),
            "" if source in {"linkedin.com", "x.com", "twitter.com", "reddit.com"} else source) if employer} if len(name.split()) > 1 else set()
        if same_person & seen:
            continue
        seen.update({key, *same_person})
        result.append(lead)
    return result
