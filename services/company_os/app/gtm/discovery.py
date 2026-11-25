import json
import re
from typing import Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    ValidationError,
    ValidationInfo,
    field_validator,
    model_validator,
)

from app.gtm.quality import MarketRole as MarketRole, PassageScores as PassageScores


class LocalDiscoveryProvider:
    def __init__(self, provider):
        self.provider = provider
        self.name = provider.name
        self._limit = provider._limit

    async def search(self, query: str, **options):
        from app.gtm.enrich import _host
        from app.research.local_providers import search
        from app.tenancy import current_or_none

        scope = current_or_none()
        rows = await search(self.provider, query, purpose="gtm.discovery.search", **options,
            workspace_id=scope.workspace_id if scope else None, venture_id=scope.venture_id if scope else None)
        exclude_domains = options.get("exclude_domains") or ()
        return [row for row in rows if not any(_host(row.get("url") or "") == domain
            or _host(row.get("url") or "").endswith("." + domain) for domain in exclude_domains)][:self._limit]

    async def scrape(self, url: str, *, main_only: bool = True):
        from app.research.local_providers import local_read

        return await local_read(url, main_only=main_only)


def local_provider(*, limit: int = 10):
    from app.research.local_providers import search_provider, search_ready

    try:
        from app.core.local_capabilities import enabled
    except ImportError:
        ready = search_ready()
    else:
        ready = enabled("find_customers") and enabled("research") and search_ready()
    provider = search_provider(limit=limit) if ready else None
    return LocalDiscoveryProvider(provider) if provider else None


# shortcut: English counts up to ten, use structured count extraction if larger spelled-out counts are needed.
SEARCH_REQUEST_RE = re.compile(r"^\s*(?:(?:ok(?:ay)?|yes|please|now|so|great|cool|go ahead and|can you|could you)[,.!\s]+)*(?:find|reach|contact|recruit|identify|locate)(?:\s+me)?\s+(?:(?:\d+|one|two|three|four|five|six|seven|eight|nine|ten)\s+)?", re.I)
REACH_TAIL_RE = re.compile(r"\s+(?:(?:who\s+|that\s+)?(?:(?:i|we)\s+)?(?:can|could)\s+|"
    r"so\s+(?:i|we)\s+(?:can|could)\s+|to\s+)(?:(?:talk|speak)\s+(?:to|with|about)|ask|interview|e-?mail|contact|reach)\b.*$", re.I)
GENERIC_ASK = frozenset(
    "a an and any all are can could current do does for from get give going has have help how i is it let lets list look me more my new next now of on our out please progress "
    "show so status tell that the their them there these this to up us want we what whats where who with would you your "
    "find finding found first search searching searches searched lead leads customer customers client clients prospect prospects people person buyer buyers user users potential prospective suitable relevant "
    "contact contacts email emails reach reachable outreach go ahead start run begin again update updates result results doing done".split())
_VENTURE_REFERENCE = re.compile(r"\s+for\s+(?:my|our|the|this|that|your)\s+(?:(?:saved|current|existing)\s+)?(?:idea|venture|project)\b", re.I)


def search_note(query: str, saved_who: str = "") -> str:
    """The request text only when it adds real detail (a count, place, kind of business, role); status and generic asks add none."""
    words = re.findall(r"[\w&'+#.-]+", query or "")
    detail = [word for word in words if word.lower().strip(".'-") not in GENERIC_ASK]
    if not detail or (saved_who and " ".join(detail).lower() in saved_who.lower()):
        return ""
    return (query or "").strip()


def request_audience(note: str) -> str:
    """The audience and place in a request, without the ask, count or later conditions."""
    from app.chat.guard import _affirmative_clauses
    from app.chat.idea_bridge import request_text
    from app.core.local_mode import is_local
    from app.intake.smart import audience_phrase

    line = note.strip().rsplit("\nLatest request: ", 1)[-1].removeprefix("Latest request: ")
    line = request_text(line)
    saved_reference = _VENTURE_REFERENCE.search(line)
    if saved_reference and not search_note(SEARCH_REQUEST_RE.sub("", line[:saved_reference.start()])):
        return ""
    line = _VENTURE_REFERENCE.sub("", line)
    clauses = _affirmative_clauses(line)
    # shortcut: sentence-led English searches keep inability predicates intact, use grounded spans for other word orders.
    line = next((clause for clause in re.split(r"(?<=[.!?])\s+|[;\n]", line) if SEARCH_REQUEST_RE.match(clause)),
                next((clause for clause in clauses if SEARCH_REQUEST_RE.match(clause)), clauses[0] if clauses else ""))
    line = SEARCH_REQUEST_RE.sub("", line)
    purpose = re.search(r"\s+for\s+", line, re.I)
    if is_local() and purpose and not search_note(line[:purpose.start()]):
        return ""
    # shortcut: English interview/conversation purposes; use grounded extraction for other display purposes.
    line = REACH_TAIL_RE.sub("", re.split(r"[,;]|[.!?](?:\s|$)|\s+for\s+"
        r"(?:(?:a|an|the|customer|user|discovery|research)\s+)*(?:interviews?|conversations?)\b",
        line, maxsplit=1, flags=re.I)[0]).strip().rstrip(".!?")
    if not search_note(line) or saved_reference and line and line.split()[-1].casefold() in GENERIC_ASK:
        return ""
    return audience_phrase(line) or line


def person_target(context: dict) -> bool:
    note = str(context.get("founder_note") or "").rsplit("\nLatest request: ", 1)[-1]
    return context.get("mode") == "B2B" and bool(REACH_TAIL_RE.search(note))


def query_text(value: str) -> str:
    value = re.sub(r'(?<!\w)-(?:[\w.-]+:)?(?:"[^"]*"|[\w.-]+)', "", value)
    value = re.sub(r"\b(?:\d[\d,.]*\s*(?:[-–—]|to)\s*\d[\d,.]*|\d[\d,.]*\+?)\s*(?:hourly\s+)?(?:staff|employees?|workers?|locations?|people|seats?|years?(?: old)?|users?)\b", "", value, flags=re.I)
    value = re.sub(r"\b\d[\d,.]*\s*(?:[-–—]|to)\s*\d[\d,.]*\b", "", value)
    return " ".join(value.split()).strip()


def contact_queries(audience: str, role: str = "", location: str = "") -> list[str]:
    scope = query_text(" ".join(str(value or "") for value in (role, audience, location)))[:180]
    return [f"{scope} {suffix}" for suffix in ("contact", "leadership", "association members", "team", "directory")]


def target_scopes(context: dict) -> list[str]:
    values = [context.get("saved_audience") or context.get("customer") or context.get("audience"),
        context.get("industry") or context.get("organisation_audience")]
    return [query_text(re.sub(r"\b[A-Z][A-Z0-9]{1,}s?\b|[()]", "", str(value))) for value in values if value]


def target_fit(text: str, context: dict) -> bool:
    from app.research.source_quality import _stems

    words = {word[:4] for word in _stems(text)}
    roles = {word[:4] for word in _stems(str(context.get("requested_role") or ""))}
    place = {word[:4] for word in _stems(str(context.get("location") or "") + " " + str(context.get("geography") or ""))}
    scopes = target_scopes(context)
    if scopes and not any(scope.strip() for scope in scopes):
        return False
    for scope in scopes:
        terms = {word[:4] for word in _stems(" ".join(word for word in scope.split() if word.casefold() not in GENERIC_ASK))} - place
        terms = terms - roles or terms
        if terms and not words & terms:
            return False
    return True


def scoped_query(query: str, context: dict) -> str:
    if target_fit(query, context):
        return query
    scope = " ".join(dict.fromkeys(" ".join(target_scopes(context)).split()))
    grounded = " ".join((scope + " " + query).split()[:24])
    return grounded if len(grounded) <= 240 else grounded[:240].rsplit(" ", 1)[0]


def hit_target_fit(hit: dict, context: dict) -> bool:
    from app.core.local_mode import is_local
    from app.gtm.enrich import _host
    from app.research.source_quality import _stems

    # Local business fit is verified on the fetched site, not its search snippet.
    if is_local() and context.get("mode") == "B2B" and hit.get("kind") == "business" and context.get("target_type") != "person":
        return True
    snippet = str(hit.get("snippet") or hit.get("description") or "")
    text = str(hit.get("title") or "") + " " + snippet
    if (context.get("mode") == "B2C" and context.get("target_type") != "person"
            and not context.get("requested_role") and not context.get("industry")
            and not context.get("organisation_audience") and hit.get("kind") == "person"
            and context.get("problem") and target_fit(text, {"audience": context["problem"]})):
        return True
    # shortcut: sparse snippets still need the existing page/model check; use grounded snippet classification if sparse mismatches become common.
    if not snippet or len(_stems(text)) < 2:
        abbreviations = re.findall(r"\b[A-Z][A-Z0-9]{1,}s?\b", " ".join(str(context.get(key) or "")
            for key in ("saved_audience", "customer", "audience", "industry", "organisation_audience")))
        shorthand = re.search(r"\b[A-Z][A-Z0-9]{1,}s?\b", text) or _stems(text) & _stems(" ".join(abbreviations))
        return not shorthand or target_fit(text, context)
    organisation = next((row for row in context.get("organisations", [])
        if _host(row["url"]) == _host(str(hit.get("url") or ""))), None)
    return target_fit(text + " " + str((organisation or {}).get("quote") or ""), context)


class Query(BaseModel):
    model_config = ConfigDict(extra="forbid")
    query: str = Field(min_length=1, max_length=240)
    intent: Literal["customer_voice", "prior_complaints", "need_hiring", "competitor_switch", "channel", "community", "business", "person"]
    expert_role: str | None = Field(default=None, max_length=160, exclude_if=lambda value: not value)

    @field_validator("query")
    @classmethod
    def natural_query(cls, value):
        value = query_text(value)
        content = re.sub(r'\b(?:site|intitle|inurl):(?:"[^"]*"|[^\s]+)', "", value)
        if not content.strip() or len(value.split()) > 24 or value.count('"') % 2 or re.search(r"\b(?:OR|AND|NOT)\b|[(){}\[\]]|(?<!\w)[+~]|\b(?!(?:site|intitle|inurl):)[\w]+:", value):
            raise ValueError("query_not_natural")
        return value


class Plan(BaseModel):
    model_config = ConfigDict(extra="forbid")
    queries: list[Query] = Field(min_length=2, max_length=8)
    target_type: Literal["person", "business"] | None = None
    audience: str = Field(default="", max_length=1000)
    requested_count: int | None = Field(default=None, ge=1)
    requested_role: str = Field(default="", max_length=160)
    organisation_audience: str = Field(default="", max_length=1000)

    @model_validator(mode="before")
    @classmethod
    def usable_queries(cls, value, info: ValidationInfo):
        if not isinstance(value, dict) or not isinstance(value.get("queries"), list):
            return value
        from app.core.local_mode import is_local
        context = info.context or {}
        query_context = context | {key: value[key] for key in ("requested_role", "organisation_audience")
            if value.get(key) and not context.get(key)}
        queries, seen, contact_index = [], set(), 0
        for row in value["queries"][:40]:
            if not isinstance(row, dict) or not isinstance(row.get("query"), str):
                continue
            query = " ".join(re.sub(r"\b(?!(?:site|intitle|inurl):)[a-zA-Z][\w-]*:", "", row["query"]).split())
            scope_context = query_context
            if (is_local() and context.get("mode") == "B2C" and isinstance(row.get("expert_role"), str)
                    and row["expert_role"] and str(row.get("intent") or "").lower() == "person"
                    and not any(context.get(key) for key in ("requested_role", "organisation_audience", "founder_query_hints"))
                    and context.get("target_type") != "person"):
                scope_context = query_context | {"saved_audience": row["expert_role"], "customer": row["expert_role"],
                    "audience": row["expert_role"], "industry": "", "requested_role": "", "organisation_audience": ""}
                variants = contact_queries(row["expert_role"], location=context.get("location") or context.get("geography") or "")
                query = variants[contact_index % len(variants)]
                contact_index += 1
            elif (is_local() and str(row.get("intent") or "").lower() in {"business", "person"}
                    and not context.get("founder_query_hints")
                    and (context.get("mode") != "B2C" or query_context.get("requested_role") or query_context.get("organisation_audience"))):
                audience = query_context.get("organisation_audience") or query_context.get("industry") or query_context.get("saved_audience") or query_context.get("audience")
                if audience:
                    role = (query_context.get("requested_role") or row.get("expert_role") or "") if row.get("intent") == "person" else ""
                    variants = contact_queries(audience, role, context.get("location") or context.get("geography") or "")
                    query = variants[contact_index % len(variants)]
                    contact_index += 1
            if info.context:
                query = scoped_query(query, scope_context)
            intent = str(row.get("intent") or "customer_voice").lower()
            if intent not in Query.model_fields["intent"].annotation.__args__:
                intent = next((kind for words, kind in (
                    (("community", "channel", "forum", "group"), "community"),
                    (("hire", "hiring", "job"), "need_hiring"), (("switch", "replace"), "competitor_switch"),
                    (("review", "complaint"), "prior_complaints")) if any(word in intent for word in words)), "customer_voice")
            try:
                checked = Query.model_validate(row | {"query": query, "intent": intent})
            except ValidationError:
                continue
            if info.context and not target_fit(checked.query, scope_context):
                continue
            if checked.query.casefold() not in seen:
                seen.add(checked.query.casefold())
                queries.append(checked.model_dump())
            if len(queries) == 8:
                break
        result = {"queries": queries, **{key: value[key] for key in ("target_type", "audience", "requested_count", "requested_role", "organisation_audience") if key in value}}
        if is_local() and context.get("audience"):
            result["audience"] = context["audience"]
        note = str(context.get("founder_note") or "")
        if (is_local() and not any(context.get(key) for key in ("target_type", "requested_role", "organisation_audience"))
                and _VENTURE_REFERENCE.search(note) and not request_audience(note)):
            result.update(target_type=None, requested_role="", organisation_audience="")
        if info.context and person_target(info.context):
            result.update(target_type="person", audience=info.context["audience"])
        return result


def people_search_message(count: int, target: dict, organisations: list[dict], *, error_code: str = "") -> str:
    requested = target.get("requested_count")
    message = f"You found {count}" + (f" of {requested}" if requested else "") + " people with public sources."
    if not requested or count < requested:
        if organisations:
            message += " You can continue with these organisations: " + "; ".join(
                f"{row['name']} ({row['url']})" for row in organisations[:5]) + ". Ask to look for people at one of them, or broaden your role."
        elif error_code in {"", "no_public_candidates"}:
            message += " Try a broader role or place, or name an organisation to search next. Nothing is sent."
    else:
        message += " Review their sources and use a Reach button to contact them. Nothing is sent."
    return normalise_note(message)


class Judgement(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int = Field(ge=0, le=87)
    kind: Literal["customer_voice", "prior_complaints", "need_hiring", "competitor_switch", "channel", "business", "person"]
    quote: str = Field(min_length=12, max_length=1000)
    affiliation: str = Field(max_length=1000)
    name: str = Field(max_length=160)
    url: str = Field(max_length=2048)
    role: str = Field(default="", max_length=160)
    company: str = Field(default="", max_length=160)
    location: str = Field(default="", max_length=160)
    fit: int = Field(default=0, ge=0, le=100)
    why: str = Field(default="", max_length=400)
    value_exchange: str = Field(default="", max_length=400)
    partner_quote: str = Field(default="", max_length=1000)
    lead_type: Literal["customer", "insight"] = "customer"

    @field_validator("why", "value_exchange", mode="before")
    @classmethod
    def value_sentence(cls, value):
        if not isinstance(value, str):
            return value
        if re.search(r"\b(?:he|him|his|she|her)\b", value, flags=re.I):
            # shortcut: discard gendered detail, use grammar-aware rewriting if that detail must be retained.
            return "Ask about their experience to see how you can help."
        value = re.sub(r"^\s*(?:accepted|accept|reject|yes|no)\b(?:\s+at\s+group\s+level)?\s*[:\-–—]\s*", "", value, flags=re.I)
        return re.sub(r"\b(?:the|this) founder\b", "you", value, flags=re.I)


class Rejection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int = Field(ge=0, le=87)
    reject_reason: str = Field(min_length=1, max_length=400, pattern=r"\S")


class Judgements(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[Judgement | Rejection] = Field(max_length=40)


def _safe_note(value: str) -> str:
    from app.autopilot import claims
    value = " ".join(value.split())
    from app.content.safety.linter import SLOP_PHRASES
    # logic adapted from ethanplusai/harvey (MIT)
    # Preserve the existing no-link boundary (stricter than a one-link ceiling); booking links are separate published routes.
    ai_tells = ("as an ai", "i'm an ai", "i am an ai", "as a language model", "hope this finds you well", "hope this email finds you well", "just wanted to", "reaching out because")
    if (any(phrase in value.casefold() for phrase in SLOP_PHRASES + ai_tells)
            or re.search(r"\{[^{}]+\}|https?://|www\.|\b\S+@\S+\b", value, re.IGNORECASE) or claims.violations(value, founder_words="")):
        raise ValueError("draft_not_safe_or_short")
    return normalise_note(value)


def normalise_note(value: str) -> str:
    value = re.sub(r"^(Hi|Hello|Hey|Dear)(\s+[^,!:]+)?[,!:]\s*", lambda match: match[0].rstrip(" ,!:\n") + ",\n\n", value, flags=re.I)
    # Founder copy uses plain punctuation; model drafts often add em/en dashes between clauses.
    value = re.sub(r"\s*[\u2014\u2013]\s*", ", ", value)
    return re.sub(r"([.!?]\s+|\s+)(reply with a time)[.!?]?\s*$", r"\1Reply with a time.", value, flags=re.I)


class Draft(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: int = Field(ge=0, le=7)
    body: str = Field(min_length=1, max_length=1500)

    @field_validator("body")
    @classmethod
    def short_honest_note(cls, value):
        value = _safe_note(value)
        if len(value.split()) > 90:
            raise ValueError("draft_not_safe_or_short")
        return value


class Drafts(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[Draft] = Field(max_length=8)


class ReachDraft(Draft):
    model_config = ConfigDict(extra="ignore")
    id: int = Field(ge=0, le=87)
    connection_note: str = Field(min_length=1, max_length=300)

    @model_validator(mode="before")
    @classmethod
    def trim_minor_overflow(cls, value):
        if not isinstance(value, dict):
            return value
        value = dict(value)
        for field, limit in (("body", 1500), ("connection_note", 300)):
            if not isinstance(value.get(field), str):
                continue
            text = _safe_note(value[field])
            if len(text) > limit * 1.2 or len(text.split()) > 110:
                raise ValueError("draft_not_safe_or_short")
            text = " ".join(text.split()[:90])
            value[field] = text if len(text) <= limit else text[:limit - 1].rsplit(" ", 1)[0].rstrip(" ,;:") + "."
        return value

    @field_validator("connection_note")
    @classmethod
    def honest_connection_note(cls, value):
        return Draft.short_honest_note(value)


class ReachDrafts(BaseModel):
    model_config = ConfigDict(extra="ignore")
    items: list[ReachDraft] = Field(max_length=8)


def parse_response(model, text: str, *, rejected=None) -> dict:
    text = text.strip()
    if text.startswith("```") and text.endswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, count=1, flags=re.I).removesuffix("```").strip()
    value = json.loads(text)
    if model in (Judgements, Drafts, ReachDrafts) and isinstance(value, dict) and isinstance(value.get("items"), list):
        item_model = TypeAdapter(model.model_fields["items"].annotation.__args__[0])
        items = []
        for row in value["items"][:40]:
            try:
                items.append(item_model.validate_python(row).model_dump())
            except ValidationError as error:
                if rejected:
                    rejected(row, [{"type": issue["type"], "loc": list(issue["loc"]), "msg": issue["msg"]}
                        for issue in error.errors(include_input=False, include_url=False, include_context=False)])
        value = value | {"items": items}
    return model.model_validate(value).model_dump()


def judge_context(context: dict) -> dict:
    from urllib.parse import urlsplit, urlunsplit

    from app.boundary import Segment
    from app.boundary.detectors import pii
    from app.feedback.normalize import delimit_untrusted, sanitize
    def without_tracking(match):
        try:
            url = urlsplit(match[0])
            return urlunsplit((url.scheme, url.netloc, url.path, "", ""))
        except ValueError:
            return "[invalid URL]"
    context = context | {"candidates": [{**row, "text": re.sub(r"https?://[^\s)\]>\"']+", without_tracking, row["text"])}
        for row in context.get("candidates", [])]}
    def redact(value, *, untrusted=False):
        if isinstance(value, dict):
            return {key: redact(item, untrusted=untrusted or key == "candidates") for key, item in value.items()}
        if isinstance(value, list):
            return [redact(item, untrusted=untrusted) for item in value]
        if isinstance(value, str):
            for finding in pii.scan(Segment(text=value)):
                if finding.value:
                    value = value.replace(finding.value, "[REDACTED]")
            if untrusted:
                return delimit_untrusted(sanitize(value))
        return value
    return redact(context)
