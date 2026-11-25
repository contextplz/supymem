"""Lead Library: match a venture's ICP to licensed leads, claim them into the workspace, and learn from outcomes.

Library leads never bypass founder approval, the daily send cap, suppression or the legal checks: claim only creates
ordinary GtmContact rows that go through the same journey gates as every other prospect.
"""
import hashlib
import json
import logging
import re
from datetime import timedelta
from urllib.parse import urlparse

from sqlalchemy import and_, case, func, not_, or_, select
from app.core.local_mode import is_local

from app.db.models import GtmCompany, GtmContact, Venture
from app.gtm.shared_models import SharedAddressValidity
from app.lead_library import taxonomy
from app.lead_library.clean import dedupe, host, normalize_email, person_key, registrable
from app.lead_library.models import LeadLibraryEntry as Entry, LeadLibraryTag as Tag, LeadLibraryUsage as Usage
from app.state import db
from core.clock import utcnow
from core.ids import new_id

log = logging.getLogger(__name__)

CAP_WORKSPACES = 3
CAP_DAYS = 30
MIN_SOFT_BOUNCE_FAILURES = 2
CLAIM_MAX = 50
VERIFIED = ("delivered", "replied")
PUBLISHED = frozenset({"public", "found", "found_public_mailto", "public_bio", "unverified_public", "published"})
_MARKER = re.compile(r":lead_library:(.+)$")
_CHANNEL_LABELS = {"email": "email", "company inbox email": "company inbox email", "linkedin": "LinkedIn", "x": "X", "phone": "phone"}
_TYPE_WORDS = (("investor", r"investor|\bvcs?\b|venture capital|angel"), ("mentor", r"mentor|advisor"),
    ("partner", r"partner|agenc|reseller|integrator"), ("media", r"journalist|media|podcast|creator|influencer"),
    ("ecosystem", r"accelerator|incubator"), ("government", r"government|public sector|ministry"), ("researcher", r"researcher|professor|academic"))
_OFFICE_ROLES = ("office", "workplace", "admin", "operations", "ops")
_SIZE_UNITS = r"(?:employees?|staff|people|persons?|team members?)"
_SOFTWARE_INDUSTRIES = ("b2b_saas_general", "devtools", "ai_ml", "cybersecurity", "enterprise_it", "consumer_apps", "gaming", "web3_crypto")
_TECH_INDUSTRIES = _SOFTWARE_INDUSTRIES + ("hardware_iot", "robotics", "semiconductors", "space_aerospace", "neurotech_bci", "telecom")
MIN_ROLE_MATCH = 0.5


class LeadLibraryError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _list(raw: str | None) -> list:
    try:
        value = json.loads(raw or "[]")
        return value if isinstance(value, list) else []
    except (TypeError, ValueError):
        return []


def _email_published(entry: Entry) -> bool:
    from app.gtm.journey import _json
    return entry.email_status in PUBLISHED or _json(entry.provenance_json).get("email_published") is True


def _set_email_status(entry: Entry, status: str) -> None:
    from app.gtm.journey import _json
    if _email_published(entry):
        entry.provenance_json = json.dumps(_json(entry.provenance_json) | {"email_published": True})
    entry.email_status = status


def _company_inbox(entry: Entry) -> bool:
    flags = set(_list(entry.quality_flags))
    if not flags & {"email_generic", "email_shared"} or flags & {"email_placeholder", "email_is_publisher", "website_is_publisher",
        "email_not_personal", "email_domain_mismatch", "email_free"} or entry.email_status == "bounced" or not _email_published(entry):
        return False
    email = normalize_email(entry.email)
    domain = registrable(host(entry.company_domain))
    if not email or not domain or email != (entry.email or "").strip().lower():
        return False
    local, email_domain = email.rsplit("@", 1)
    return (registrable(email_domain) == domain and domain not in taxonomy.FREE_MAIL | taxonomy.PLACEHOLDER_DOMAINS | taxonomy.PUBLISHER_HOSTS
        and local not in taxonomy.PLACEHOLDER_LOCALS)


def channels(entry: Entry) -> list[str]:
    """Offer only supported email routes; delivery alone is not publication evidence."""
    flagged = bool(set(_list(entry.quality_flags)) & {"email_generic", "email_shared"})
    routes = {"email": entry.email, "linkedin": entry.linkedin_url, "x": entry.x_url, "phone": entry.phone}
    usable = [channel for channel in _list(entry.usable_channels)
        if routes.get(channel) and channel != "company inbox email" and not (channel == "email" and (flagged or entry.email_status == "bounced" or not _email_published(entry)))]
    return usable + (["company inbox email"] if _company_inbox(entry) else [])


def _company_inbox_ids(session, *where) -> list[str]:
    # ponytail: scans flagged candidates at read time, persist an indexed tier if matching latency grows.
    candidates = session.scalars(select(Entry).where(Entry.status == "active", *where,
        or_(Entry.quality_flags.contains('"email_generic"'), Entry.quality_flags.contains('"email_shared"'))))
    return [entry.id for entry in candidates if _company_inbox(entry)]


def _hash(email: str | None) -> str | None:
    from app.gtm.email_learning import opaque
    try:
        return opaque(email) if email else None
    except ValueError:
        return None


def _normalize_sizes(text: str) -> str:
    return re.sub(r"\b(micro|small|medium|large|mid)[- ]?siz(?:e|ed)\b",
        lambda match: "medium" if match[1] == "mid" else match[1], text)


def _company_size(value, *, explicit: bool = False):
    if not isinstance(value, (str, int)) or isinstance(value, bool):
        return None
    text = _normalize_sizes(str(value or "").lower().replace(",", ""))
    if re.search(r"\b(?:under|over|about|around|at least|at most|up to|fewer than|more than)\s+\d|\d\s*\+", text):
        return None
    units = "" if explicit else rf"\s+{_SIZE_UNITS}\b"
    bounds = re.search(rf"(?<![\d.-])\b(\d+)\s*(?:-|–|to)\s*(\d+)\b{units}", text)
    if bounds:
        lower, upper = int(bounds[1]), int(bounds[2])
        return (lower, upper) if 0 < lower <= upper else None
    count = re.search(rf"(?<![\d.-])\b(\d+)\s+{_SIZE_UNITS}\b", text)
    if count:
        number = int(count[1])
        return (number, number) if number > 0 else None
    if explicit and re.fullmatch(r"\d+", text.strip()):
        number = int(text)
        return (number, number) if number > 0 else None
    label = re.search(r"\b(micro|small|medium|large)\b", text)
    return label[1] if label else None


def _size_matches(provenance: str, requested) -> bool:
    try:
        saved = json.loads(provenance or "{}")
    except (TypeError, ValueError):
        return False
    if not isinstance(saved, dict):
        return False
    sizes = [_company_size(saved.get(key), explicit=True) for key in ("company_size", "employee_count", "headcount")]
    known = [size for size in sizes if size is not None]
    if isinstance(requested, str):
        return bool(known) and all(size == requested for size in known)
    return bool(known) and all(isinstance(size, tuple) and requested[0] <= size[0] <= size[1] <= requested[1] for size in known)


def _role_tokens(title: str) -> set[str]:
    words = re.findall(r"[a-z]+", title.lower())
    ignored = {"a", "an", "and", "at", "of", "the", "for", "leader", "leaders"}
    return {re.sub(r"ies$", "y", word) if word.endswith("ies") else word.removesuffix("s").removesuffix("ing")
        for word in words if word not in ignored}


def _role_score(title: str | None, found: dict) -> float:
    actual = _role_tokens(title or "")
    requested = _role_tokens(found["role"])
    if not actual or not requested:
        return 0.0
    title_initials = "".join(word[0] for word in re.findall(r"[a-z]+", (title or "").lower()) if word not in {"of", "and", "the"})
    role_initials = "".join(word[0] for word in re.findall(r"[a-z]+", found["role"].lower()) if word not in {"of", "and", "the"})
    if requested == {title_initials} or actual == {role_initials}:
        return 1.0
    if found["role_terms"]:
        return float(any(word.startswith(term) for word in actual for term in _role_tokens(" ".join(found["role_terms"]))))
    if requested == _role_tokens(found["function"] or ""):
        return float(any(pattern.search(title or "") for name, pattern in taxonomy._FUNCTION_RE if name == found["function"]))
    # shortcut: require title-token coverage beyond existing taxonomy aliases; use cached semantic matching when available.
    if not requested <= actual:
        return 0.0
    return 2 * len(requested) / (len(requested) + len(actual))


def _location_matches(entry: Entry, found: dict) -> bool:
    saved = taxonomy.parse_geography(entry.geography_raw)
    for key in ("region", "city"):
        requested = str(found[key] or "").lower()
        actual = str(getattr(entry, key) or saved[key] or "").lower()
        if key == "region" and found["country"] == "US":
            actual = taxonomy.US_STATES.get(actual, actual).lower()
            requested = taxonomy.US_STATES.get(requested, requested).lower()
        if requested and requested != actual:
            return False
    return True


def filters(icp: dict) -> dict:
    """Deterministic ICP -> taxonomy filters; the same rules that classified the library."""
    who = str(icp.get("who") or "")
    normalized = _normalize_sizes(re.sub(r"\b(founders|owners|heads|managers|leaders)\b", lambda match: match[0][:-1], who.lower()))
    audience = re.split(r"\b(?:at|in|for)\b", normalized, maxsplit=1)
    role = audience[0]
    if taxonomy.function_for(role) == "founder_exec":
        ownership = re.split(r"\bof\b", role, maxsplit=1)
        if len(ownership) > 1:
            audience = [ownership[0], ownership[1] + (" in " + audience[1] if len(audience) > 1 else "")]
            role = audience[0]
    company = re.split(r"\bwho\b", audience[1], maxsplit=1)[0] if len(audience) > 1 else ""
    function, seniority = taxonomy.function_for(role), taxonomy.seniority_for(role)
    if not company:
        title_rules = [pattern for name, pattern in taxonomy._FUNCTION_RE if name == function]
        title_rules += [pattern for name, pattern in taxonomy._SENIORITY_RE if name == seniority]
        if function == "operations":
            title_rules.append(re.compile(r"\b(?:" + "|".join(_OFFICE_ROLES) + r")\w*\b", re.I))
        title_spans = [hit.span() for pattern in title_rules for hit in pattern.finditer(role)]
        company = re.sub(r"\b\w+\b", lambda word: "" if any(start < word.end() and end > word.start()
            for start, end in title_spans) else word[0], role)
    industry_text = _normalize_sizes(str(icp.get("industry") or re.split(r"\b(?:with|in|across|from)\b", company, maxsplit=1)[0]).lower())
    technology = bool(re.search(r"\btech(?:nology)?\b", industry_text, re.I))
    software = bool(re.search(r"\bsoftware\b", industry_text, re.I))
    industry_text = re.sub(r"\btech(?:nology)?\b", "software", industry_text, flags=re.I)
    industries = taxonomy.industries_for(taxonomy.tokens(industry_text))
    if industries == ["b2b_saas_general"] and not re.search(r"\b(?:b2b|saas)\b", industry_text, re.I):
        if technology:
            industries = list(_TECH_INDUSTRIES)
        elif software:
            industries = list(_SOFTWARE_INDUSTRIES)
    industry_terms = []
    if industry_text:
        ignored = {"small", "micro", "medium", "large", "startup", "startups", "company", "companies", "business", "businesses",
            "practice", "practices", "firm", "firms", "with", "staff", "employee", "employees", "people", "team", "members", "the",
            "early", "stage", "tools", "independent", "mid", "size", "sized", "midsize", "app", "apps", "software", "technology"}
        covered = [hit.span() for _, pattern in taxonomy._INDUSTRY_RE for hit in pattern.finditer(industry_text)]
        words = [word for word in re.finditer(r"\b[a-z]{3,}\b", industry_text)
            if word[0] not in ignored and not taxonomy.parse_geography(word[0])["country_code"]]
        industry_terms = [word[0] for word in words if not any(start < word.end() and end > word.start() for start, end in covered)]
        if not industry_terms and industries:
            aliases = _role_tokens(" ".join(industries + [taxonomy.LABELS[industry] for industry in industries]))
            industry_terms = [word[0] for word in words if not _role_tokens(word[0]) <= aliases]
        # shortcut: lexical specialty stems only; use reviewed semantic tags when synonyms are needed.
        industry_terms = sorted(word.removesuffix("er") for word in _role_tokens(" ".join(industry_terms)))
    types = [name for name, pattern in _TYPE_WORDS if re.search(pattern, who, re.I)] or ["customer_prospect", "founder", "operator"]
    location = str(icp.get("location") or icp.get("geography") or "").strip()
    geo = taxonomy.parse_geography(location)
    if location.lower() not in taxonomy._UNKNOWN_GEO and not any(geo.values()):
        geo["city"] = location
    for phrase in re.split(r"\b(?:based in|in|across|from)\s+(?:the\s+)?", who, flags=re.I)[1:]:
        inferred = taxonomy.parse_geography(re.split(r"\b(?:who|with|for|at)\b", phrase, maxsplit=1, flags=re.I)[0].strip())
        geo = {key: geo[key] or inferred[key] for key in geo}
    if re.search(r"\bleader\b", role):
        seniority = "unknown"
    role_terms = list(_OFFICE_ROLES) if function == "operations" and any(term in role for term in _OFFICE_ROLES) else []
    if function == "marketing" and "growth" in role:
        role_terms = ["growth"]
    if function == "founder_exec" and seniority == "founder_owner":
        role_terms = ["founder", "owner", "proprietor"]
    if len(audience) == 1 and not role_terms and not icp.get("industry"):
        industry_terms = []
    stage = taxonomy.stage_for(company)
    size_value = icp.get("company_size")
    size_stated = size_value is not None and size_value != ""
    size = _company_size(size_value, explicit=True) if size_stated else _company_size(company)
    if size is None and (size_stated or re.search(rf"\d[^a-z]*{_SIZE_UNITS}\b", company)):
        size = "unknown"
    # Buyer tags describe what they need, so only the problem and offer count; "startups" in the audience is not a need.
    need = " ".join(str(icp.get(k) or "") for k in ("problem", "offer"))
    return {"industry": industries, "industry_terms": industry_terms, "buyer_tag": taxonomy.buyer_tags_for(need), "lead_types": types,
        "country": geo["country_code"], "region": geo["region"], "city": geo["city"],
        "role": re.split(r"\b(?:who|with)\b", role, maxsplit=1)[0].strip(), "function": None if function == "other" else function,
        "seniority": None if seniority == "unknown" else seniority, "stage": None if stage == "unknown" else stage,
        "role_terms": role_terms, "company_size": size}


def _visible(workspace_id: str, now, company_inboxes=()):
    """Active, usable, not globally suppressed or retired, and under the cross-workspace cap."""
    if is_local():
        return (False,)
    retired_since = now - timedelta(days=90)
    capped = (select(Usage.entry_id).where(Usage.outcome == "sent", Usage.contacted_at >= now - timedelta(days=CAP_DAYS),
        Usage.workspace_id != workspace_id).group_by(Usage.entry_id).having(func.count(func.distinct(Usage.workspace_id)) >= CAP_WORKSPACES))
    blocked = select(SharedAddressValidity.address_hash).where(or_(SharedAddressValidity.suppressed.is_(True), SharedAddressValidity.retired_at >= retired_since))
    stopped = or_(Entry.status == "suppressed", Entry.email_hash.in_(blocked), Entry.id.in_(capped))
    shared_routes = []
    for route, usable in ((Entry.email_hash, True), (Entry.linkedin_url, Entry.usable_channels.contains('"linkedin"')),
            (Entry.x_url, Entry.usable_channels.contains('"x"'))):
        normalized = func.rtrim(func.lower(route), "/")
        stopped_routes = select(normalized).where(route.is_not(None), usable, stopped).correlate(None)
        shared_routes.append(or_(not_(usable), route.is_(None), normalized.not_in(stopped_routes)))
    return (Entry.status == "active", or_(Entry.usable_channels != "[]", Entry.id.in_(company_inboxes)),
        not_(and_(Entry.usable_channels == '["email"]', Entry.email_status == "bounced")),
        or_(Entry.email_hash.is_(None), Entry.email_hash.not_in(blocked)), Entry.id.not_in(capped), *shared_routes)


def _icp(venture_id: str) -> dict:
    from app.gtm import journey
    with db()() as session:
        venture = session.get(Venture, venture_id)
        if venture is None:
            raise LeadLibraryError("This venture is unavailable.", 404)
        workspace_id = venture.workspace_id
    return journey.status(workspace_id, venture_id, authorize=False)["icp"]


def match(venture_id: str, *, limit: int = 5, offset: int = 0, icp: dict | None = None) -> dict:
    """Require the stated audience constraints before ranking usable library leads."""
    from app.core.local_mode import is_local
    from app.gtm.journey import _consumer
    if is_local():
        return {"total": 0, "items": [], "filters": filters(icp or {})}
    icp = icp if icp is not None else _icp(venture_id)
    found = filters(icp)
    empty = {"total": 0, "items": [], "filters": found}
    if found["company_size"] == "unknown" or _consumer(icp) or not _role_tokens(found["role"]):
        return empty
    with db()() as session:
        venture = session.get(Venture, venture_id)
        if venture is None:
            raise LeadLibraryError("This venture is unavailable.", 404)
        total, items = ranked(session, venture.workspace_id, venture_id, found, limit=limit, offset=offset)
    return {"total": total, "items": items, "filters": found}


def ranked(session, workspace_id: str, venture_id: str, found: dict, *, limit: int, offset: int) -> tuple[int, list[dict]]:
    """Apply audience constraints before channel tiers, tag overlap, delivery and quality."""
    if is_local():
        return 0, []
    now = utcnow()
    facets = [and_(Tag.tag_type == kind, Tag.tag.in_(found[kind])) for kind in ("industry", "buyer_tag") if found[kind]]
    wanted = or_(*facets) if facets else False
    score = func.sum(case((Tag.tag_type == "industry", 2), (Tag.tag_type == "buyer_tag", 1), else_=0)) + sum((case((column == found[key], 1), else_=0) for key, column in
        (("function", Entry.function), ("seniority", Entry.seniority), ("stage", Entry.company_stage)) if found[key]), 0)
    claimed = select(Usage.entry_id).where(Usage.venture_id == venture_id, Usage.outcome == "claimed")
    scope = (Entry.lead_type.in_(found["lead_types"]), Entry.id.not_in(claimed),
        *((Entry.country_code == found["country"],) if found["country"] else ()),
        *((Entry.seniority == found["seniority"],) if found["seniority"] else ()),
        *((Entry.company_stage == found["stage"],) if found["stage"] else ()))
    if found["function"]:
        function = Entry.function == found["function"]
        if found["function"] == "engineering":
            title = func.lower(Entry.role_title)
            for separator in ("/", "&", ",", "-"):
                title = func.replace(title, separator, " ")
            function = or_(function, and_(Entry.function == "founder_exec",
                or_((" " + title + " ").like("% cto %"), Entry.role_title.ilike("%engineer%"))))
        scope += (function,)
    if found["role_terms"]:
        scope += (or_(*(Entry.role_title.ilike(f"%{term}%") for term in found["role_terms"])),)
    if found["industry"]:
        scope += (Entry.id.in_(select(Tag.entry_id).where(Tag.tag_type == "industry", Tag.tag.in_(found["industry"]))),)
    for term in found["industry_terms"]:
        scope += (Entry.id.in_(select(Tag.entry_id).where(
            Tag.tag_type.in_(("category_raw", "niche_raw")), Tag.tag.ilike(f"%{term}%"))),)
    inboxes = _company_inbox_ids(session, *scope)
    where = (*_visible(workspace_id, now, inboxes), *scope)
    # shortcut: score narrowed candidates at read time; index title tokens if candidate scans become slow.
    role_scores = {}
    candidates = []
    for entry in session.scalars(select(Entry).where(*where)):
        role_score = _role_score(entry.role_title, found)
        usable = channels(entry)
        if usable and role_score >= MIN_ROLE_MATCH and _location_matches(entry, found) and (
            not found["company_size"] or _size_matches(entry.provenance_json, found["company_size"])):
            role_scores[entry.id] = role_score
            candidates.append({"lead_id": entry.id, "dedupe_keys": {"lead_id": entry.external_lead_id,
                "email": normalize_email(entry.email) if "email" in usable else None,
                "linkedin": entry.linkedin_url.lower().rstrip("/") if "linkedin" in usable else None,
                "x": entry.x_url.lower().rstrip("/") if "x" in usable else None,
                "person_company": person_key(entry.person_name, entry.company)},
                "usable_channels": usable, "quality_flags": _list(entry.quality_flags),
                "quality_score": entry.quality_score, "fit_score": entry.fit_score})
    if not role_scores:
        return 0, []
    survivors, _ = dedupe(candidates)
    role_scores = {entry["lead_id"]: role_scores[entry["lead_id"]] for entry in survivors}
    where += (Entry.id.in_(role_scores),)
    role_rank = case(role_scores, value=Entry.id, else_=0)
    lower_priority = and_(Entry.id.in_(inboxes), not_(and_(Entry.usable_channels.contains('"linkedin"'),
        func.coalesce(Entry.linkedin_url, "") != "")))
    grouped = (Entry.id, Entry.function, Entry.seniority, Entry.company_stage, Entry.email_status, Entry.fit_score, Entry.quality_score, Entry.usable_channels)
    page = session.execute(select(Entry.id, func.count().over()).outerjoin(Tag, and_(Tag.entry_id == Entry.id, wanted)).where(*where)
        .group_by(*grouped).order_by(case((lower_priority, 1), else_=0), role_rank.desc(), score.desc(), case((Entry.email_status.in_(VERIFIED), 1), else_=0).desc(),
            func.coalesce(Entry.fit_score, -1).desc(), func.coalesce(Entry.quality_score, -1).desc(), Entry.id)
        .limit(max(1, min(limit, 50))).offset(max(0, offset))).all()
    if page:
        total = page[0][1]
    else:
        total = session.scalar(select(func.count(Entry.id)).where(*where)) or 0
    ids = [entry_id for entry_id, _ in page]
    entries = {entry.id: entry for entry in session.scalars(select(Entry).where(Entry.id.in_(ids)))} if ids else {}
    hits: dict[str, list[str]] = {}
    reasons = and_(Tag.tag_type == "industry", Tag.tag.in_(found["industry"])) if found["industry"] else (
        False if found["industry_terms"] else wanted if facets else False)
    for entry_id, tag in session.execute(select(Tag.entry_id, Tag.tag).where(Tag.entry_id.in_(ids), reasons)) if ids else ():
        hits.setdefault(entry_id, []).append(tag)
    return total, [_item(entries[i], hits.get(i, []), found) for i in ids]


def _item(entry: Entry, tags: list[str], found: dict) -> dict:
    reasons = []
    if found["function"] and entry.function == found["function"]:
        reasons.append(taxonomy.LABELS[entry.function])
    if tags:
        reasons.append(", ".join(taxonomy.LABELS.get(t, t.replace("_", " ")) for t in sorted(set(tags))[:3]))
    if found["country"] and entry.country_code == found["country"]:
        reasons.append(entry.country_code)
    if entry.email_status in VERIFIED:
        reasons.append("email " + entry.email_status + " before")
    return {"id": entry.id, "name": entry.person_name, "role": entry.role_title or "", "company": entry.company or "",
        "why": "; ".join(reasons), "channels": [_CHANNEL_LABELS[c] for c in channels(entry)], "country": entry.country_code}


def _note(entry: Entry) -> str:
    host = entry.source_host or (urlparse(entry.source_url or "").hostname or "").removeprefix("www.")
    return f"Licensed business contact list ({entry.license_source}). Listed at {host or 'the licensed source'}."


def claim(workspace_id: str, venture_id: str, entry_ids: list[str], actor: str | None = None) -> dict:
    """Copy library entries into this venture's prospects. Idempotent; unusable channels are never copied."""
    from app.core.local_mode import is_local
    if is_local():
        raise LeadLibraryError("You cannot use purchased contacts locally. Ask me to search public sources for your customers, or import your own contacts you have permission to use in Contacts > Import.", 403)
    from app.autopilot.send_admission import try_send_admission
    from app.db.session import serialize_writes
    from app.gtm.god.generation import authority
    from app.gtm.supymem.pipeline import _persist_lead
    actor = authority(workspace_id, venture_id, write=True, actor=actor)
    ids = list(dict.fromkeys(str(i) for i in entry_ids if i))[:CLAIM_MAX]
    result = {"claimed": 0, "already": 0, "unavailable": 0, "contact_ids": [], "email_drafts": 0,
        "channel_contact_ids": {channel: [] for channel in _CHANNEL_LABELS}}
    now = utcnow()
    with try_send_admission(venture_id) as admitted, db()() as session:
        if not admitted:
            raise LeadLibraryError("Wait for the current outreach send to finish, then add these again.", 409)
        serialize_writes(session, key="lead-library-claim:" + workspace_id)
        venture = session.get(Venture, venture_id)
        if not venture or venture.workspace_id != workspace_id or venture.archived_at:
            raise LeadLibraryError("This venture is unavailable.", 404)
        inboxes = _company_inbox_ids(session, Entry.id.in_(ids)) if ids else []
        entries = {entry.id: entry for entry in session.scalars(select(Entry).where(Entry.id.in_(ids), *_visible(workspace_id, now, inboxes))) if channels(entry)} if ids else {}
        result["unavailable"] = len(ids) - len(entries)
        for entry in (entries[i] for i in ids if i in entries):
            source = f"venture:{venture_id}:lead_library:{entry.external_lead_id}"
            usable = channels(entry)
            if not usable:
                result["unavailable"] += 1
                continue
            company_inbox = "company inbox email" in usable
            email = entry.email if "email" in usable or company_inbox else None
            linkedin = entry.linkedin_url if "linkedin" in usable else None
            contact = session.scalar(select(GtmContact).where(GtmContact.workspace_id == workspace_id, or_(GtmContact.source == source,
                func.lower(GtmContact.email) == email.lower() if email else False,
                func.lower(GtmContact.linkedin_url) == linkedin.lower() if linkedin else False)).limit(1))
            if contact:
                from app.gtm.journey import _json
                evidence = _json(contact.enrichment_json)
                if not (contact.source or "").startswith(f"venture:{venture_id}:") or not evidence.get("skipped"):
                    result["already"] += 1
                    continue
                evidence["skipped"] = False
                contact.enrichment_json = json.dumps(evidence)
                result["contact_ids"].append(contact.id)
                result["claimed"] += 1
                continue
            name = entry.person_name.split(maxsplit=1)
            x_handle = (urlparse(entry.x_url).path.strip("/").split("/") or [""])[0] if entry.x_url and "x" in usable else None
            contact = _persist_lead(session, workspace_id, {"email": email, "first_name": name[0] if name else None, "last_name": name[1] if len(name) > 1 else None,
                "title": entry.role_title or "Contact", "organization": entry.company, "company": entry.company, "website": entry.website,
                "domain": entry.company_domain, "linkedin_url": linkedin, "x_handle": x_handle, "source": source,
                "source_url": entry.source_url, "email_confidence": "guess" if company_inbox else "risky" if email else "none"}, schedule_research=False)
            contact.contact_provenance = "lead_library"
            # lawful_basis_at stays empty: EU/UK library leads need the founder to record permission before email.
            contact.lawful_basis, contact.lawful_basis_at, contact.lawful_basis_note = "legitimate_interest", None, _note(entry)
            contact.recipient_geo = entry.country_code
            contact.enrichment_json = json.dumps({"business": entry.company, "website": entry.website, "person_name": entry.person_name,
                "why": "From Supymem's lead library.", "found_at": entry.source_url or entry.website, "source_url": entry.source_url,
                **({"email_evidence": "company_inbox"} if company_inbox else {}),
                **({"phone": entry.phone} if "phone" in usable else {}),
                "lead_library": {"entry_id": entry.id, "external_lead_id": entry.external_lead_id, "license_source": entry.license_source,
                    "email_tier": "company_inbox" if company_inbox else "personal" if email else "none",
                    "email_status": entry.email_status, "email_published": _email_published(entry), "quality_flags": _list(entry.quality_flags),
                    "batch_id": entry.batch_id, "discovery_method": entry.discovery_method, "collected_at": entry.collected_at,
                    "claimed_at": now.isoformat(), "claimed_by": actor}})
            session.add(Usage(id=new_id("llu"), entry_id=entry.id, workspace_id=workspace_id, venture_id=venture_id, contacted_at=now, outcome="claimed"))
            result["contact_ids"].append(contact.id)
            for channel, value in {"email": contact.email, "linkedin": contact.linkedin_url, "x": contact.x_handle,
                                   "phone": entry.phone if "phone" in usable else None}.items():
                if value:
                    result["channel_contact_ids"][channel].append(contact.id)
            result["claimed"] += 1
        session.commit()
    if result["channel_contact_ids"]["email"]:
        from app.autopilot import outreach
        from app.intake.contacts import _prepare_drafts
        result["drafts_status"] = _prepare_drafts(workspace_id, venture_id)
        try:
            result["email_drafts"] = len({draft["contact_id"] for draft in outreach.drafts(venture_id, "draft")
                if draft.get("contact_id") in result["channel_contact_ids"]["email"]
                and draft.get("channel") == "email" and draft.get("day") == 1})
        except Exception as exc:
            log.warning("lead_library_drafts_unreadable", extra={"error_class": type(exc).__name__})
            result["email_drafts"] = None
    return result


def _entry_for(session, contact: GtmContact) -> Entry | None:
    if is_local():
        return None
    marker = _MARKER.search(contact.source or "")
    if marker:
        return session.scalar(select(Entry).where(Entry.external_lead_id == marker[1]))
    address = _hash(contact.email)
    return session.scalar(select(Entry).where(Entry.email_hash == address).limit(1)) if address else None


def _same_address(entry: Entry, contact: GtmContact) -> bool:
    return bool(entry.email and contact.email and entry.email.casefold() == contact.email.strip().casefold()) \
        or bool(entry.email_hash and entry.email_hash == _hash(contact.email))


def over_cap(session, contact: GtmContact) -> bool:
    """Send-time cross-workspace cap for library leads; follow-ups from a workspace already in the thread still go."""
    marker = _MARKER.search(contact.source or "")
    if not marker:
        return False
    entry = session.scalar(select(Entry.id).where(Entry.external_lead_id == marker[1]))
    if entry is None:
        return False
    senders = set(session.scalars(select(Usage.workspace_id).where(Usage.entry_id == entry, Usage.outcome == "sent",
        Usage.contacted_at >= utcnow() - timedelta(days=CAP_DAYS))))
    return contact.workspace_id not in senders and len(senders) >= CAP_WORKSPACES


def _guarded(fn):
    """Hooks run inside send/bounce/opt-out transactions: a library failure must never undo those writes."""
    def run(session, *args, **kwargs):
        if is_local():
            return
        try:
            with session.begin_nested():
                fn(session, *args, **kwargs)
        except Exception:  # noqa: BLE001 - logged; the caller's outcome is the source of truth
            log.exception("lead_library_hook_failed", extra={"hook": fn.__name__})
    return run


@_guarded
def on_outcome(session, contact: GtmContact, kind: str, observed_at=None) -> None:
    """Shared usage, delivery/reply promotion and address-level bounce blacklisting."""
    from app.core.local_mode import is_local
    if is_local():
        return
    if kind not in {"send", "delivery", "reply", "hard_bounce", "soft_bounce"}:
        return
    now = observed_at or utcnow()
    entry = _entry_for(session, contact)
    if entry is None:
        if kind in {"reply", "delivery"}:
            promote(session, contact, "replied" if kind == "reply" else "delivered")
        return
    venture = (contact.source or "").split(":")[1] if (contact.source or "").startswith("venture:") else None
    if kind == "send":
        session.add(Usage(id=new_id("llu"), entry_id=entry.id, workspace_id=contact.workspace_id, venture_id=venture, contacted_at=now, outcome="sent"))
    elif _same_address(entry, contact):
        if kind in {"hard_bounce", "soft_bounce"}:
            address = _hash(contact.email)
            siblings = list(session.scalars(select(Entry).where(or_(Entry.email_hash == address if address else False,
                func.lower(Entry.email) == contact.email.strip().lower())).order_by(Entry.id).with_for_update()))
            session.info.pop("gtm_shared_suppression", None)
            if kind == "soft_bounce":
                from app.gtm.email_learning import _aware
                sent_at = _aware(contact.last_emailed_at) if contact.last_emailed_at else None
                prefix = "llu_soft_send_" if sent_at else "llu_soft_unknown_"
                key = prefix + hashlib.sha256(json.dumps([address or contact.email.strip().lower(), contact.workspace_id,
                    sent_at.isoformat() if sent_at else "unknown"]).encode()).hexdigest()[:24]
                if session.get(Usage, key) is None:
                    session.add(Usage(id=key, entry_id=entry.id, workspace_id=contact.workspace_id, venture_id=venture,
                        contacted_at=sent_at or now, outcome=kind))
                session.flush()
                failures = list(session.scalars(select(Usage).where(Usage.entry_id.in_([sibling.id for sibling in siblings]), Usage.outcome == kind)))
                founders = {failure.workspace_id for failure in failures}
                sends = {_aware(failure.contacted_at) for failure in failures if failure.id.startswith("llu_soft_send_")}
                if len(founders) < MIN_SOFT_BOUNCE_FAILURES and (len(sends) < MIN_SOFT_BOUNCE_FAILURES
                        or max(sends) - min(sends) < timedelta(hours=24)):
                    return
            for sibling in siblings:
                sibling.email_status = "bounced"
                if sibling.status == "active":
                    sibling.status, sibling.status_reason = "retired", "email_bounced"
            if address:
                validity = session.get(SharedAddressValidity, address)
                if validity is None:
                    validity = SharedAddressValidity(address_hash=address, suppressed=False, last_seen=now)
                    session.add(validity)
                validity.status, validity.retired_at = "retired", now
        elif entry.status == "active" and entry.email_status != "bounced":
            if kind == "reply" or entry.email_status != "replied":
                _set_email_status(entry, "replied" if kind == "reply" else "delivered")
        if kind not in {"delivery", "soft_bounce"}:
            session.add(Usage(id=new_id("llu"), entry_id=entry.id, workspace_id=contact.workspace_id, venture_id=venture, contacted_at=now, outcome=kind))


@_guarded
def on_suppress(session, email: str) -> None:
    """email_learning.suppress hook: an opt-out from any workspace hides the person from every workspace at once."""
    from app.core.local_mode import is_local
    if is_local():
        return
    address = _hash(email)
    clause = Entry.email_hash == address if address else func.lower(Entry.email) == email.strip().lower()
    for entry in session.scalars(select(Entry).where(clause, Entry.status == "active")):
        entry.status, entry.status_reason = "suppressed", "opt_out"


def promote(session, contact: GtmContact, outcome: str, *, entries=None, companies=None) -> Entry | None:
    """Add a working public lead to the library. Founder imports and pasted contacts are never promoted."""
    if is_local():
        return None
    from app.gtm.journey import _json
    from app.gtm.scrapy_leads import _seed_url
    source = contact.source or ""
    evidence = _json(contact.enrichment_json)
    if contact.contact_provenance or not source.startswith("venture:") or "founder_import" in source or "lead_library" in source:
        return None
    found_at = str(evidence.get("found_at") or "") if _seed_url(str(evidence.get("found_at") or "")) else ""
    name = str(evidence.get("person_name") or " ".join(p for p in (contact.first_name, contact.last_name) if p)).strip()
    email = (contact.email or "").strip().lower()
    company = (companies.get(contact.company_id) if companies is not None else session.get(GtmCompany, contact.company_id)) if contact.company_id else None
    domain = registrable((company.domain if company else "") or (urlparse(str(evidence.get("website") or "")).hostname or ""))
    if not (found_at and name and email and domain) or registrable(email.rsplit("@", 1)[-1]) != domain or domain in taxonomy.FREE_MAIL:
        return None
    address = _hash(email)
    if address is None:
        return None
    entry = entries.get(address) if entries is not None else session.scalar(select(Entry).where(Entry.email_hash == address).limit(1))
    if entry is not None:
        if entry.email_status not in VERIFIED or outcome == "replied":
            _set_email_status(entry, outcome)
        return entry
    # Only an address published on the public source page is stored; a pattern guess keeps its hash and outcome only.
    published = evidence.get("email_evidence") == "published" or email in str(evidence.get("contact_quote") or "").lower()
    role = contact.role or ""
    company_name = (company.canonical_name if company else "") or str(evidence.get("business") or "")
    text = " ".join((company_name, str(evidence.get("company_text") or "")[:500]))
    industries = taxonomy.industries_for(taxonomy.tokens(company_name), text)[:4]
    tags = taxonomy.buyer_tags_for(" ".join((role, text)))[:8]
    usable = (["email"] if published else []) + (["linkedin"] if contact.linkedin_url else [])
    host = (urlparse(found_at).hostname or "").removeprefix("www.")
    row = {"person_name": name, "role_title": role or None, "company": company_name or None, "company_domain": domain, "source_url": found_at,
        "country_code": contact.recipient_geo, "email_status": outcome}
    entry = Entry(id="ll_" + hashlib.sha256(("verified:" + address).encode()).hexdigest()[:24], external_lead_id="verified_" + address[:24],
        **row, website=evidence.get("website"), linkedin_url=contact.linkedin_url, email=email if published else None, email_hash=address,
        lead_type="customer_prospect", function=taxonomy.function_for(role), seniority=taxonomy.seniority_for(role), company_stage="unknown",
        confidence=0.6, usable_channels=json.dumps(usable), quality_flags="[]", provenance_json=json.dumps({"promoted_from": outcome, "email_published": published}),
        source_host=host, discovery_method="supymem_outcome", license_source=taxonomy.VERIFIED_SOURCE, imported_at=utcnow(),
        content_hash=hashlib.sha256(json.dumps(row, sort_keys=True).encode()).hexdigest(), status="active",
        search_text=" ".join(str(v) for v in (name, role, company_name, domain, *industries, *tags) if v).lower())
    session.add(entry)
    if entries is not None:
        entries[address] = entry
    session.add_all([Tag(entry_id=entry.id, tag_type="industry", tag=t) for t in industries] + [Tag(entry_id=entry.id, tag_type="buyer_tag", tag=t) for t in tags])
    return entry


def sweep(now=None) -> dict:
    """Nightly: hide suppressed and retired addresses, and promote leads delivered 72h ago with no bounce."""
    if is_local():
        return {"suppressed": 0, "retired": 0, "promoted": 0}
    from app.gtm.email_learning import _active_retirement, _aware
    now = now or utcnow()
    counts = {"suppressed": 0, "retired": 0, "promoted": 0}
    import os
    size = max(1, min(1000, int(os.getenv("LEAD_SWEEP_BATCH_SIZE", "200"))))
    cursor = ""
    while True:
        with db()() as session:
            rows = session.execute(select(Entry, SharedAddressValidity).join(SharedAddressValidity,
                SharedAddressValidity.address_hash == Entry.email_hash).where(Entry.id > cursor,
                Entry.status == "active", or_(SharedAddressValidity.suppressed.is_(True),
                SharedAddressValidity.retired_at.is_not(None))).order_by(Entry.id).limit(size)).all()
            if not rows:
                break
            for entry, validity in rows:
                if validity.suppressed:
                    entry.status, entry.status_reason = "suppressed", "opt_out"
                    counts["suppressed"] += 1
                elif _active_retirement(validity, _aware(now)):
                    entry.status, entry.status_reason = "retired", "address_retired"
                    counts["retired"] += 1
            cursor = rows[-1][0].id
            session.commit()
    cursor = ""
    while True:
        with db()() as session:
            sent = list(session.scalars(select(GtmContact).where(GtmContact.id > cursor,
                GtmContact.times_emailed > 0, GtmContact.bounced_at.is_(None), GtmContact.email.is_not(None),
                GtmContact.last_emailed_at <= now - timedelta(hours=72),
                GtmContact.last_emailed_at >= now - timedelta(days=CAP_DAYS)).order_by(GtmContact.id).limit(size)))
            if not sent:
                break
            hashes = {_hash(contact.email) for contact in sent} - {None}
            markers = {match[1] for contact in sent if (match := _MARKER.search(contact.source or ""))}
            validities = {row.address_hash: row for row in session.scalars(select(SharedAddressValidity).where(
                SharedAddressValidity.address_hash.in_(hashes)))}
            entries, by_marker = {}, {}
            for entry in session.scalars(select(Entry).where(or_(Entry.email_hash.in_(hashes), Entry.external_lead_id.in_(markers)))):
                entries.setdefault(entry.email_hash, entry)
                by_marker[entry.external_lead_id] = entry
            companies = {row.id: row for row in session.scalars(select(GtmCompany).where(
                GtmCompany.id.in_({contact.company_id for contact in sent if contact.company_id})))}
            for contact in sent:
                address = _hash(contact.email)
                validity = validities.get(address)
                if not validity or validity.status != "deliverable" or validity.suppressed or _active_retirement(validity, _aware(now)):
                    continue
                marker = _MARKER.search(contact.source or "")
                entry = by_marker.get(marker[1]) if marker else entries.get(address)
                if entry is not None and _same_address(entry, contact):
                    if entry.email_status not in VERIFIED:
                        _set_email_status(entry, "delivered")
                        counts["promoted"] += 1
                elif entry is None and promote(session, contact, "delivered", entries=entries, companies=companies) is not None:
                    counts["promoted"] += 1
            cursor = sent[-1].id
            session.commit()
    return counts
