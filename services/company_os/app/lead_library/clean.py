"""Deterministic Lead Library cleaning: contact fields are only normalized or marked unusable, never invented."""
from __future__ import annotations

import hashlib
import json
import re
from collections import Counter, defaultdict
from urllib.parse import urlparse

from app.lead_library import taxonomy as tx

_EMAIL = re.compile(r"^[a-z0-9._%+'-]+@[a-z0-9.-]+\.[a-z]{2,}$")
_STOP = {"inc", "ltd", "llc", "pvt", "private", "limited", "technologies", "technology", "tech", "labs", "lab", "ai", "the", "co", "corp",
    "corporation", "company", "solutions", "systems", "group", "global", "india", "software", "services", "hq", "and", "of", "io", "app",
    "health", "studio", "ventures", "capital", "partners", "network", "digital", "innovations", "innovation", "foundation"}
_NOT_A_NAME = {"team", "contact", "founders", "company", "founding team", "info", "admin", "office", "unknown", "n/a", "na", "none",
    "test", "platform", "leadership", "investment team", "platform team", "operations team", "venture team"}
_NON_PERSON = re.compile(r"^\+\s*\d+\s*more$|\b(desk|team|office|helpdesk|department|cell|committee|secretariat)\b|@", re.I)
_SLD = {"co", "com", "org", "net", "ac", "gov", "edu", "nic", "res", "gen", "firm", "ind"}


def host(url: str | None) -> str:
    text = str(url or "").strip()
    if not text:
        return ""
    if "//" not in text:
        text = "https://" + text
    try:
        name = (urlparse(text).hostname or "").lower()
    except ValueError:
        return ""
    return name.removeprefix("www.").removeprefix("m.").strip(".")


def registrable(name: str) -> str:
    parts = [part for part in (name or "").split(".") if part]
    if len(parts) >= 3 and parts[-2] in _SLD and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _label(domain: str) -> str:
    reg = registrable(domain)
    return re.sub(r"[^a-z0-9]", "", reg.split(".")[0]) if reg else ""


def name_tokens(value: str | None) -> list[str]:
    return [token for token in re.findall(r"[a-z0-9]+", (value or "").lower()) if len(token) >= 3 and token not in _STOP]


def company_matches(company: str | None, domain: str | None) -> bool:
    label = _label(domain or "")
    if len(label) < 2:
        return False
    compact = re.sub(r"[^a-z0-9]", "", (company or "").lower())
    words = re.findall(r"[a-z0-9]+", (company or "").lower())
    initials = "".join(word[0] for word in words if word not in {"the", "and", "of"})
    if compact and (label in compact or compact in label):
        return True
    if len(initials) >= 2 and (label == initials or label.startswith(initials)):
        return True
    return any(token in label for token in name_tokens(company) if len(token) >= 4)


def personal_local(local: str, person: str | None) -> bool:
    """The mailbox name is the person's own: contains a name part, or initials plus a name part, or bare initials."""
    words = [word for word in re.findall(r"[a-z]+", re.sub(r"\b(dr|mr|mrs|ms|prof)\.?\s", "", (person or "").lower())) if word]
    if not words or not local:
        return False
    compact = re.sub(r"[^a-z]", "", local.lower())
    if any(len(word) >= 3 and word in compact for word in words):
        return True
    initials = "".join(word[0] for word in words)
    return len(initials) >= 2 and compact in {initials, initials[0] + initials[-1]}


def normalize_url(value: str | None) -> str | None:
    text = str(value or "").strip().strip("<>\"'")
    if not text or " " in text or "." not in text:
        return None
    if not re.match(r"^https?://", text, re.I):
        text = "https://" + text.lstrip("/")
    return text if host(text) else None


def normalize_email(value: str | None) -> str | None:
    text = str(value or "").strip().lower().removeprefix("mailto:")
    text = re.split(r"[\s,;/|]+", text)[0] if text else ""
    text = text.split("?")[0].strip(".<>()[]\"'")
    return text if _EMAIL.match(text) else None


def normalize_phone(value: str | None) -> str | None:
    text = str(value or "").strip()
    if not text:
        return None
    first = re.split(r"[,;/|]| or ", text)[0]
    digits = re.sub(r"\D", "", first)
    if not 8 <= len(digits) <= 15:
        return None
    return ("+" if first.strip().startswith("+") else "") + digits


def normalize_linkedin(value: str | None) -> tuple[str | None, bool]:
    url = normalize_url(value)
    if not url or "linkedin.com" not in host(url):
        return None, False
    path = urlparse(url).path.rstrip("/")
    match = re.match(r"^/(in|pub|company|school|showcase)/([^/?#]+)", path, re.I)
    if not match:
        return None, False
    kind, slug = match[1].lower(), match[2]
    return f"https://www.linkedin.com/{kind}/{slug}", kind in {"company", "school", "showcase"}


def normalize_x(url: str | None, handle: str | None) -> str | None:
    value = normalize_url(url)
    if value and host(value) in {"x.com", "twitter.com"}:
        name = urlparse(value).path.strip("/").split("/")[0]
    else:
        name = str(handle or "").strip().lstrip("@").split("/")[-1]
    name = name.strip()
    return f"https://x.com/{name}" if re.fullmatch(r"[A-Za-z0-9_]{1,15}", name or "") and name.lower() not in {"home", "intent", "share", "i"} else None


def person_key(name: str | None, company: str | None) -> str:
    person = " ".join(re.findall(r"[a-z0-9]+", re.sub(r"\b(dr|mr|mrs|ms|prof)\.?\s", "", (name or "").lower())))
    org = " ".join(name_tokens(company)) or re.sub(r"[^a-z0-9]", "", (company or "").lower())
    numbers = re.findall(r"\b\d+\b", company or "")
    if numbers and org:
        org += "|" + " ".join(numbers)
    return f"{person}|{org}" if person and org else ""


def fit_scales(rows: list[dict]) -> dict[str, int]:
    top: dict[str, float] = defaultdict(float)
    for row in rows:
        try:
            top[str(row.get("batch_id") or "")] = max(top[str(row.get("batch_id") or "")], float(row.get("fit_score")))
        except (TypeError, ValueError):
            continue
    return {batch: 10 if value <= 10 else 100 for batch, value in top.items()}


def _float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def prepare(rows: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (kept leads, dropped rows with reasons). Every input row lands in exactly one list."""
    scales = fit_scales(rows)
    base = []
    for row in rows:
        website = normalize_url(row.get("website"))
        linkedin, linkedin_company = normalize_linkedin(row.get("linkedin_url"))
        base.append({"row": row, "website": website, "website_host": host(website), "source_host": host(row.get("source_url")),
            "email": normalize_email(row.get("email")), "phone": normalize_phone(row.get("phone")), "linkedin": linkedin,
            "linkedin_company": linkedin_company, "x": normalize_x(row.get("x_url"), row.get("x_handle"))})
    companies_by_host: dict[str, set] = defaultdict(set)
    source_companies: dict[str, set] = defaultdict(set)
    for item in base:
        company = " ".join(name_tokens(item["row"].get("company"))) or (item["row"].get("company") or "").lower()
        if item["website_host"]:
            companies_by_host[registrable(item["website_host"])].add(company)
        if item["source_host"]:
            source_companies[registrable(item["source_host"])].add(company)
    people = lambda field: {key: len(value) for key, value in _group(base, field).items()}  # noqa: E731
    email_people, phone_people, x_people, linkedin_people = people("email"), people("phone"), people("x"), people("linkedin")

    def publisher_host(name: str, company: str | None, source: str) -> bool:
        reg = registrable(name)
        if not reg:
            return False
        if name in tx.PUBLISHER_HOSTS or reg in tx.PUBLISHER_HOSTS:
            return True
        if company_matches(company, name):
            return False
        return len(companies_by_host[reg]) >= 3 or (reg == registrable(source) and len(source_companies[reg]) >= 3)

    kept, dropped = [], []
    for item in base:
        row = item["row"]
        lead_id = str(row.get("lead_id") or "").strip()
        name = re.sub(r"\s+", " ", str(row.get("person_name") or "")).strip()
        company = re.sub(r"\s+", " ", str(row.get("company") or "")).strip() or None
        if not lead_id:
            dropped.append({"lead_id": None, "reason": "missing_lead_id"})
            continue
        if not name:
            dropped.append({"lead_id": lead_id, "reason": "missing_person_name"})
            continue
        if name.lower() in _NOT_A_NAME or _NON_PERSON.search(name) or not re.search(r"[^\W\d_]", name):
            dropped.append({"lead_id": lead_id, "reason": "name_not_a_person"})
            continue
        flags = []
        website_publisher = bool(item["website_host"]) and publisher_host(item["website_host"], company, item["source_host"])
        if website_publisher:
            flags.append("website_is_publisher")
        email = item["email"]
        email_domain = email.rsplit("@", 1)[1] if email else ""
        local = email.split("@")[0] if email else ""
        company_domain = None if website_publisher or not item["website_host"] else registrable(item["website_host"])
        if email:
            free = email_domain in tx.FREE_MAIL
            placeholder = email_domain in tx.PLACEHOLDER_DOMAINS or local in tx.PLACEHOLDER_LOCALS
            publisher = not free and not company_matches(company, email_domain) and (
                publisher_host(email_domain, company, item["source_host"]) or (website_publisher and registrable(email_domain) == registrable(item["website_host"])))
            generic = local in tx.GENERIC_LOCALS or local.split(".")[0] in tx.GENERIC_LOCALS or local.split("+")[0] in tx.GENERIC_LOCALS
            matches = bool(company_domain and registrable(email_domain) == company_domain) or company_matches(company, email_domain)
            flags += [flag for flag, on in (("email_is_publisher", publisher), ("email_generic", generic), ("email_placeholder", placeholder),
                ("email_free", free), ("email_shared", email_people.get(email, 0) > 1),
                ("email_domain_mismatch", not free and not placeholder and not matches)) if on]
            if company_domain is None and matches and not publisher and not placeholder and not free:
                company_domain = registrable(email_domain)
        if item["linkedin_company"]:
            flags.append("linkedin_is_company")
        x_company = bool(item["x"]) and (x_people.get(item["x"], 0) > 1 or (company_matches(company, item["x"].rsplit("/", 1)[-1] + ".x")
            and not any(token in item["x"].lower() for token in name_tokens(name))))
        phone_company = bool(item["phone"]) and phone_people.get(item["phone"], 0) > 1
        if x_company or phone_company or item["linkedin_company"]:
            flags.append("contact_company_level")
        role = re.sub(r"\s+", " ", str(row.get("role_title") or "")).strip()
        if not role or role.lower() in {"missing", "unknown", "n/a", "na", "none", "-", "company", "contact", "team"}:
            flags.append("missing_role")
        geo = tx.parse_geography(row.get("geography"))
        if not geo["country_code"]:
            flags.append("geography_unknown")
        personal = bool(email) and personal_local(local, name)
        if email and not personal and "email_generic" not in flags:
            flags.append("email_not_personal")
        email_ok = bool(email) and personal and not set(flags) & {"email_is_publisher", "email_generic", "email_placeholder", "email_shared"} \
            and ("email_free" in flags or "email_domain_mismatch" not in flags)
        channels = [channel for channel, ok in (
            ("email", email_ok),
            ("linkedin", bool(item["linkedin"]) and not item["linkedin_company"] and linkedin_people.get(item["linkedin"], 0) <= 1),
            ("x", bool(item["x"]) and not x_company),
            ("phone", bool(item["phone"]) and not phone_company)) if ok]
        scale = scales.get(str(row.get("batch_id") or ""), 100)
        fit_raw = _float(row.get("fit_score"))
        fit = None if fit_raw is None else max(0, min(100, round(fit_raw * (10 if scale == 10 else 1))))
        lead = {
            "lead_id": lead_id, "person_name": name, "role_title": role or None, "company": company, "company_domain": company_domain,
            "website": None if website_publisher else item["website"], "website_host": item["website_host"] or None,
            "linkedin_url": item["linkedin"], "x_url": item["x"], "email": email, "email_status": (row.get("email_status") or "none"),
            "phone": item["phone"], "phone_status": (row.get("phone_status") or "none"), **geo,
            "geography_raw": row.get("geography"), "lead_type_raw": row.get("lead_type"), "lead_types_raw": row.get("lead_types"),
            "category": row.get("category"), "categories": row.get("categories"), "subcategory": row.get("subcategory"),
            "focus_niche": row.get("focus_niche"), "qualification_notes": row.get("qualification_notes"),
            "score_rationale": row.get("score_rationale"), "fit_score": fit, "fit_score_raw": row.get("fit_score"), "fit_score_scale": scale,
            "quality_score": _float(row.get("quality_score")), "source_url": row.get("source_url"), "source_host": item["source_host"] or None,
            "discovery_method": row.get("discovery_method"), "batch_id": row.get("batch_id"), "collected_at": row.get("collected_at"),
            "license_source": tx.LICENSE_SOURCE, "name_is_company": bool(company and name.lower() == company.lower()), "quality_flags": flags, "usable_channels": channels,
            "dedupe_keys": {"lead_id": lead_id, "email": email if email_ok else None, "person_company": person_key(name, company) or None},
        }
        kept.append(lead)
    survivors, duplicates = dedupe(kept)
    return survivors, dropped + duplicates


def _group(base, field):
    groups: dict[str, set] = defaultdict(set)
    for item in base:
        if item[field]:
            groups[item[field]].add(person_key(item["row"].get("person_name"), "x") or str(item["row"].get("lead_id")))
    return groups


def _richness(lead: dict) -> tuple:
    return (len(lead["usable_channels"]), -len(lead["quality_flags"]), lead.get("quality_score") or 0, lead.get("fit_score") or 0,
        len(json.dumps(lead, default=str)))


def dedupe(leads: list[dict]) -> tuple[list[dict], list[dict]]:
    """Union leads sharing an original lead_id, a usable personal route, or person+company. Keeps the richest record."""
    parent = list(range(len(leads)))

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    for key in ("lead_id", "email", "linkedin", "x", "person_company"):
        first: dict[str, int] = {}
        for index, lead in enumerate(leads):
            value = lead["dedupe_keys"].get(key)
            if not value:
                continue
            if value in first:
                parent[find(index)] = find(first[value])
            else:
                first[value] = index
    groups: dict[int, list[int]] = defaultdict(list)
    for index in range(len(leads)):
        groups[find(index)].append(index)
    survivors, duplicates = [], []
    for members in groups.values():
        best = max(members, key=lambda index: _richness(leads[index]))
        keep = leads[best]
        merged = sorted({leads[index]["lead_id"] for index in members} - {keep["lead_id"]})
        keep["merged_lead_ids"] = merged
        survivors.append(keep)
        for index in members:
            if index != best:
                duplicates.append({"lead_id": leads[index]["lead_id"], "reason": "duplicate", "duplicate_of": keep["lead_id"]})
    survivors.sort(key=lambda lead: lead["lead_id"])
    return survivors, duplicates


def content_hash(entry: dict) -> str:
    return hashlib.sha256(json.dumps(entry, sort_keys=True, default=str).encode()).hexdigest()


def flag_counts(leads: list[dict]) -> Counter:
    return Counter(flag for lead in leads for flag in lead["quality_flags"])
