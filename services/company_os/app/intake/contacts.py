"""Bounded, founder-reviewed contact imports into the existing GTM journey."""
from __future__ import annotations

import asyncio
import csv
import hashlib
import io
import json
import logging
import re
from datetime import datetime, timedelta, timezone
from email.utils import getaddresses

import httpx
from sqlalchemy import func, or_, select

from app.db.models import GtmContact, GtmTouch, Venture, VentureDocument
from app.db.session import serialize_writes
from app.gtm import journey
from app.gtm.god.generation import authority
from app.state import db
from core.clock import utcnow
from core.ids import new_id

MAX_ROWS = 2000
MAX_BYTES = 2 * 1024 * 1024
log = logging.getLogger(__name__)
FIELDS = ("name", "email", "company", "title", "linkedin_url", "notes", "status", "last_contacted", "country")
_ADD_PERSON_RE = re.compile(
    r"\s*(?:(?:please|can you|could you)\s+)?add\s+(?:someone\s+i\s+know\s*:\s*(?P<known>.*)|"
    r"(?P<description>.+?)\s+to\s+(?:(?:my|our|the)\s+)?(?:contacts|leads|contact\s+list|address\s+book)"
    r"(?:\s*[.!?]\s*(?P<notes>.*))?)\s*", re.IGNORECASE | re.DOTALL,
)
ALIASES = {
    "name": {"name", "fullname", "contactname"}, "email": {"email", "emailaddress", "workemail"},
    "company": {"company", "organization", "organisation"}, "title": {"title", "role", "jobtitle"},
    "linkedin_url": {"linkedin", "linkedinurl", "linkedinprofile"}, "notes": {"notes", "note"},
    "status": {"status", "outreachstatus"}, "last_contacted": {"lastcontacted", "lastcontactedat", "lastemailedat"},
    "country": {"country", "countrycode", "recipientgeo"},
}


class ContactImportError(ValueError):
    def __init__(self, message: str, status: int = 422):
        self.message, self.status = message, status
        super().__init__(message)


def _date(value: str) -> str | None:
    if not value:
        return None
    try:
        date = datetime.fromisoformat(value.replace("Z", "+00:00"))
        date = date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date.astimezone(timezone.utc)
        if date > utcnow().replace(tzinfo=timezone.utc):
            raise ValueError()
        return date.isoformat()
    except ValueError:
        raise ContactImportError("Use a past date like 2026-10-01 for last contacted.") from None


def _email(value: str) -> str:
    email = value.strip().lower()
    if not email:
        return ""
    if len(email) > 254 or email.count("@") != 1:
        raise ContactImportError("Check the email address.")
    local, domain = email.rsplit("@", 1)
    if (not re.fullmatch(r"[a-z0-9!#$%&'*+/=?^_`{|}~.-]{1,64}", local)
            or local.startswith(".") or local.endswith(".") or ".." in local
            or not re.fullmatch(r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}", domain)):
        raise ContactImportError("Check the email address.")
    return email


def parse_contacts(text: str, *, format: str = "csv", mapping: dict | None = None) -> dict:
    try:
        size = len(text.encode("utf-8"))
    except UnicodeError:
        raise ContactImportError("Use UTF-8 text for the contacts.") from None
    if not text.strip() or size > MAX_BYTES:
        raise ContactImportError("Choose a non-empty CSV or paste up to 2 MB of contacts.")
    if format not in {"csv", "paste"}:
        raise ContactImportError("Choose CSV or pasted contacts.")
    if format == "csv":
        try:
            try:
                delimiter = csv.Sniffer().sniff(text[:4096], delimiters=",;\t").delimiter
            except csv.Error:
                heading = text.splitlines()[0]
                delimiter = max(",;\t", key=heading.count)
            reader = csv.reader(io.StringIO(text.lstrip("\ufeff")), delimiter=delimiter, strict=True)
            columns = next(reader)
            if not columns or len(columns) > 32 or len(set(columns)) != len(columns) or any(not c.strip() or len(c) > 150 for c in columns):
                raise ContactImportError("Use unique, non-empty column headings.")
            detected = {field: next((c for c in columns if re.sub(r"[^a-z]", "", c.lower()) in names), "") for field, names in ALIASES.items()}
            if mapping:
                if set(mapping) - set(FIELDS) or any(value and value not in columns for value in mapping.values()):
                    raise ContactImportError("Choose a column from this CSV for each field.")
                detected.update(mapping)
            if not detected["email"] and not detected["linkedin_url"]:
                raise ContactImportError("Map an email or LinkedIn column to continue.")
            items = []
            for cells in reader:
                if not any(cells):
                    continue
                items.append(({field: cells[columns.index(column)] if column and columns.index(column) < len(cells) else "" for field, column in detected.items()}, len(cells) != len(columns)))
                if len(items) > MAX_ROWS:
                    raise ContactImportError("Import up to 2,000 contacts at a time.")
        except (csv.Error, StopIteration):
            raise ContactImportError("Check the CSV format and include a heading row.") from None
    else:
        columns, detected, items = [], {}, []
        for line in text.splitlines():
            if not line.strip():
                continue
            linkedin = line.strip() if re.fullmatch(r"https://(?:www\.)?linkedin\.com/in/[a-z0-9_%\-]+/?", line.strip(), re.I) else ""
            addresses = [("", "")] if linkedin else getaddresses([line.strip()])
            for name, email in addresses:
                items.append(({"name": name, "email": email, "linkedin_url": linkedin}, False))
                if len(items) > MAX_ROWS:
                    raise ContactImportError("Import up to 2,000 contacts at a time.")
    if not items:
        raise ContactImportError("Add at least one contact below the headings.")
    rows, seen = [], set()
    for number, (item, malformed) in enumerate(items, 1):
        row = {field: str(item.get(field) or "").strip() for field in FIELDS}
        errors = ["This row has a different number of columns."] if malformed else []
        if any("\x00" in row[field] for field in FIELDS):
            errors.append("Remove unsupported characters from this row.")
        if any(len(row[field]) > (2000 if field in {"notes", "linkedin_url"} else 254) for field in FIELDS):
            errors.append("Shorten this row's fields before importing.")
        try:
            row["email"] = _email(row["email"])
        except ContactImportError as exc:
            errors.append(exc.message)
        if row["linkedin_url"] and not re.fullmatch(r"https://(?:www\.)?linkedin\.com/in/[a-z0-9_%\-]+/?", row["linkedin_url"], re.I):
            errors.append("Use a LinkedIn profile link starting with https://.")
        if row["linkedin_url"] and not errors:
            from app.gtm.supymem.brightdata import normalize_linkedin_url
            row["linkedin_url"] = normalize_linkedin_url(row["linkedin_url"].lower()) or ""
        if not row["email"] and not row["linkedin_url"]:
            errors.append("Add an email address or LinkedIn profile.")
        row["status"] = row["status"].lower() or "new"
        if row["status"] not in {"new", "contacted", "replied", "interested"}:
            errors.append("Use new, contacted, replied, or interested for status.")
        try:
            row["last_contacted"] = _date(row["last_contacted"])
        except ContactImportError as exc:
            errors.append(exc.message)
        row["country"] = row["country"].upper()
        if row["country"] and not re.fullmatch(r"[A-Z]{2}", row["country"]):
            errors.append("Use a two-letter country code, such as US or CA.")
        identities = {"email:" + row["email"]} if row["email"] else set()
        if row["linkedin_url"]:
            identities.add("linkedin:" + row["linkedin_url"].lower())
        duplicate = bool(identities & seen)
        if not errors:
            seen.update(identities)
        rows.append(row | {"row_number": number, "errors": errors, "duplicate": duplicate, "email_check": "unknown" if row["email"] else "not_applicable"})
    return {"columns": columns, "mapping": detected, "rows": rows}


async def check_domains(domains: set[str]) -> dict[str, str]:
    """Query only domains, with a fixed resolver, bounded concurrency and deadline."""
    from app.core.local_mode import is_local
    synthetic = {domain for domain in domains if domain.endswith(".invalid") and is_local()}
    results = {domain: "unknown" for domain in domains}
    domains = domains - synthetic
    if not domains:
        return results
    gate = asyncio.Semaphore(8)
    async with httpx.AsyncClient(timeout=1.5, follow_redirects=False, trust_env=False) as client:
        async def check(domain):
            async with gate:
                try:
                    response = await client.get("https://cloudflare-dns.com/dns-query", params={"name": domain, "type": "MX"}, headers={"Accept": "application/dns-json"})
                    response.raise_for_status()
                    if len(response.content) > 64 * 1024:
                        return
                    data = response.json()
                    if data.get("Status") == 3:
                        results[domain] = "missing_mx"
                    elif data.get("Status") == 0 and not data.get("TC"):
                        answers = [a for a in data.get("Answer", []) if a.get("type") == 15]
                        results[domain] = "mx" if any(str(a.get("data", "")).split()[-1:] not in ([], ["."]) for a in answers) else "missing_mx"
                except (httpx.HTTPError, ValueError, TypeError, AttributeError):
                    pass
        try:
            async with asyncio.timeout(8):
                await asyncio.gather(*(check(domain) for domain in sorted(domains)))
        except TimeoutError:
            pass
    return results


def _preview_kind(actor: str) -> str:
    return "contact_import_preview:" + hashlib.sha256(actor.encode()).hexdigest()[:24]


def _local_importable(contact: GtmContact, vid: str) -> bool:
    from app.core.local_mode import is_local
    return (is_local()
        and contact.source in {f"venture:{vid}:public", f"venture:{vid}:founder", f"venture:{vid}:founder_import"})


def _apply_history(contact: GtmContact, status: str, last_contacted: str | None, *, merge: bool = False) -> None:
    """Founder-reported history has no provider thread or delivery receipt."""
    now = utcnow()
    stage = {"new": "new", "contacted": "congratulated", "replied": "conversing", "interested": "offered"}[status]
    emailed_at = datetime.fromisoformat(last_contacted) if last_contacted else None
    replied_at = (emailed_at or now) if status in {"replied", "interested"} else None
    count = int(bool(contact.email) and status != "new")
    if merge:
        count = max(contact.times_emailed or 0, count)
        replied_at = contact.replied_at or replied_at
        emailed_at = max((date for date in (contact.last_emailed_at, emailed_at) if date),
            key=lambda date: date.replace(tzinfo=None), default=None)
    contact.times_emailed, contact.last_emailed_at, contact.replied_at = count, emailed_at, replied_at
    stages = {"new": 0, "congratulated": 1, "conversing": 2, "offered": 3}
    if merge and stages[stage] < stages.get(contact.relationship_stage, 3):
        return
    contact.relationship_stage = stage
    contact.stage_changed_at = now
    contact.next_action_kind = {"contacted": "follow_up", "replied": "reply", "interested": "handoff"}.get(status)
    contact.next_action_at = contact.last_emailed_at + timedelta(days=3) if status == "contacted" and contact.last_emailed_at else now if status in {"replied", "interested"} else None


def _prepare_drafts(wid: str, vid: str) -> str:
    try:
        drafts = journey.status(wid, vid, authorize=False).get("drafts") or []
        return "ready" if any(draft.get("state") == "draft" for draft in drafts) else "pending"
    except Exception as exc:
        log.warning("contact_import_drafts_pending", extra={"error_class": type(exc).__name__})
        return "pending"


def _view(data: dict) -> dict:
    rows = data["rows"]
    return {key: data[key] for key in ("preview_id", "columns", "mapping", "expires_at")} | {
        "rows": rows, "total_rows": len(rows), "valid_rows": sum(not r["errors"] and not r["duplicate"] for r in rows),
        "invalid_rows": sum(bool(r["errors"]) for r in rows), "duplicates": sum(r["duplicate"] for r in rows),
    }


async def _checked_contacts(wid: str, vid: str, text: str, *, format: str, mapping: dict | None, actor: str) -> tuple[dict, dict[str, str]]:
    authority(wid, vid, write=True, actor=actor)
    with db()() as session:
        if journey._processing_block(session, wid):
            raise ContactImportError("Review the workspace outreach policy before importing contacts.", 409)
    data = parse_contacts(text, format=format, mapping=mapping)
    checks = await check_domains({r["email"].split("@", 1)[1] for r in data["rows"] if r["email"] and not r["errors"] and not r["duplicate"]})
    return data, checks


async def preview(wid: str, vid: str, text: str, *, format: str, mapping: dict | None, actor: str) -> dict:
    data, checks = await _checked_contacts(wid, vid, text, format=format, mapping=mapping, actor=actor)
    with db()() as session:
        requested_emails = {r["email"] for r in data["rows"] if r["email"] and not r["errors"]}
        requested_linkedin = {variant.lower() for r in data["rows"] if r["linkedin_url"] and not r["errors"] for variant in (r["linkedin_url"], r["linkedin_url"].rstrip("/"))}
        existing = list(session.scalars(select(GtmContact).where(GtmContact.workspace_id == wid,
            or_(GtmContact.email.in_(requested_emails), func.lower(GtmContact.linkedin_url).in_(requested_linkedin)))))
        existing = [contact for contact in existing if not _local_importable(contact, vid)
                    or (contact.contact_provenance == "founder_import"
                        and contact.lawful_basis in {"consent", "legitimate_interest"}
                        and journey._json(contact.enrichment_json).get("founder_import", {}).get("lawful_basis_attested") is True)]
        emails = {contact.email.lower() for contact in existing if contact.email}
        linkedin = {contact.linkedin_url.lower().rstrip("/") for contact in existing if contact.linkedin_url}
        for row in data["rows"]:
            row["duplicate"] = row["duplicate"] or bool(row["email"] and row["email"] in emails or row["linkedin_url"] and row["linkedin_url"].lower().rstrip("/") in linkedin)
            if row["email"] and not row["errors"]:
                row["email_check"] = checks.get(row["email"].split("@", 1)[1], "unknown")
                if row["email_check"] == "missing_mx":
                    row["errors"].append("This email domain has no mail service. Check the address.")
        now = utcnow()
        data.update(preview_id=new_id("cimp"), actor=actor, expires_at=(now + timedelta(minutes=30)).isoformat(), committed=None)
        row = session.scalar(select(VentureDocument).where(VentureDocument.venture_id == vid, VentureDocument.kind == _preview_kind(actor)))
        if row is None:
            row = VentureDocument(id=new_id("doc"), venture_id=vid, kind=_preview_kind(actor), title="Contact import preview", body_md="", created_at=now, updated_at=now)
            session.add(row)
        row.provenance_json, row.updated_at = json.dumps(data), now
        session.commit()
    return _view(data)


def described_person(text: str) -> dict | None:
    match = _ADD_PERSON_RE.fullmatch(text)
    if not match:
        return None
    description = match["known"] if match["known"] is not None else match["description"] + " " + (match["notes"] or "")
    addresses = list(re.finditer(r"[^\s,;<>]+@[^\s,;<>]+", description))
    if len(addresses) != 1:
        raise ContactImportError("Give me one email address for the person you want to add.")
    address = addresses[0]
    try:
        email = _email(address[0].lstrip("(\"'").rstrip(".?!:)\"'"))
    except ContactImportError:
        raise ContactImportError("Give me a valid email address for the person you want to add.") from None
    details = description[:address.start()].strip(" ,;:<>")
    if match["description"] is not None:
        details = re.sub(r"\s+at$", "", details, flags=re.IGNORECASE)
    name, separator, position = details.partition(",")
    if not separator and re.search(r"\s+at\s+", name, re.IGNORECASE):
        name, position = "", name
    job = re.split(r"\s+at\s+", position.strip(), maxsplit=1, flags=re.IGNORECASE)
    return {"name": name.strip(), "email": email, "title": job[0], "company": job[1] if len(job) > 1 else "",
            "notes": description[address.end():].lstrip(" ,.;:!?<>)\"'")}


async def add_person(wid: str, vid: str, person: dict, *, actor: str) -> dict:
    pasted = io.StringIO()
    csv.writer(pasted).writerows([FIELDS, [person.get(field, "") for field in FIELDS]])
    data, checks = await _checked_contacts(wid, vid, pasted.getvalue(), format="csv", mapping=None, actor=actor)
    item = data["rows"][0]
    if item["errors"] or not item["email"]:
        raise ContactImportError("Check the email address and details you gave me, then ask me to add them again.")
    item["email_check"] = checks.get(item["email"].split("@", 1)[1], "unknown")
    if item["email_check"] == "missing_mx":
        raise ContactImportError("Give me an email address that can receive mail for the person you want to add.")
    from app.autopilot.send_admission import try_send_admission
    from app.gtm.supymem.pipeline import _persist_lead

    with try_send_admission(vid) as admitted, db()() as session:
        if not admitted:
            raise ContactImportError("Wait for your current email to finish, then ask to add this person again.", 409)
        serialize_writes(session, key="founder-contact-import:" + wid)
        venture = session.get(Venture, vid, with_for_update=True)
        if not venture or venture.workspace_id != wid or venture.archived_at:
            raise ContactImportError("Open an available company, then ask to add this person again.", 404)
        if journey._processing_block(session, wid):
            raise ContactImportError("Review your workspace outreach policy, then ask to add this person again.", 409)
        existing = session.scalar(select(GtmContact).where(GtmContact.workspace_id == wid,
            func.lower(GtmContact.email) == item["email"]))
        if existing:
            return {"email": existing.email, "added": False}
        name = item["name"].split(maxsplit=1)
        contact = _persist_lead(session, wid, item | {"first_name": name[0] if name else None,
            "last_name": name[1] if len(name) > 1 else None, "source": f"venture:{vid}:founder_import",
            "email_confidence": "risky" if item["email_check"] == "mx" else "guess"}, schedule_research=False)
        contact.contact_provenance = "founder_import"
        contact.lawful_basis, contact.lawful_basis_at, contact.lawful_basis_note = "none", None, None
        evidence = journey._json(contact.enrichment_json)
        evidence["founder_import"] = {"imported_at": utcnow().isoformat(), "added_by": actor,
            "lawful_basis_attested": False, "status": "new", "last_contacted": None,
            "notes": item["notes"], "email_check": item["email_check"]}
        contact.enrichment_json = json.dumps(evidence)
        session.commit()
    from app.ventures.stage import reassess

    reassess(vid)
    return {"email": item["email"], "added": True}


def commit(wid: str, vid: str, preview_id: str, *, actor: str, lawful_basis_attested: bool,
           lawful_basis: str, lawful_basis_note: str = "") -> dict:
    authority(wid, vid, write=True, actor=actor)
    if lawful_basis_attested is not True or lawful_basis not in {"consent", "legitimate_interest"}:
        raise ContactImportError("Open Find customers > Import your existing contacts, select your lawful basis under Basis for contacting, record Evidence of permission, then attest your permission and import again.")
    if "\x00" in lawful_basis_note:
        raise ContactImportError("Remove unsupported characters from the permission note.")
    try:
        lawful_basis_note.encode("utf-8")
    except UnicodeError:
        raise ContactImportError("Use UTF-8 text for the permission note.") from None
    from app.autopilot.send_admission import try_send_admission
    from app.gtm.supymem.pipeline import _persist_lead
    with try_send_admission(vid) as admitted, db()() as session:
        if not admitted:
            raise ContactImportError("Wait for the current outreach send to finish, then import again.", 409)
        serialize_writes(session, key="founder-contact-import:" + wid)
        venture = session.get(Venture, vid, with_for_update=True)
        if not venture or venture.workspace_id != wid or venture.archived_at:
            raise ContactImportError("This venture is unavailable.", 404)
        if journey._processing_block(session, wid):
            raise ContactImportError("Review the workspace outreach policy before importing contacts.", 409)
        row = session.scalar(select(VentureDocument).where(VentureDocument.venture_id == vid, VentureDocument.kind == _preview_kind(actor)).with_for_update())
        data = journey._json(row.provenance_json) if row else {}
        if data.get("preview_id") != preview_id or data.get("actor") != actor:
            raise ContactImportError("Preview these contacts again before importing.", 409)
        replayed = bool(data.get("committed"))
        if replayed:
            result = data["committed"]
        else:
            if datetime.fromisoformat(data["expires_at"]).replace(tzinfo=None) < utcnow().replace(tzinfo=None):
                raise ContactImportError("This preview expired. Preview the contacts again.", 409)
            result = {"imported": 0, "skipped": 0, "contact_ids": [], "drafts_status": "not_requested"}
            for item in data["rows"]:
                if item["errors"]:
                    result["skipped"] += 1
                    continue
                matches = list(session.scalars(select(GtmContact).where(GtmContact.workspace_id == wid,
                    or_(GtmContact.email == item["email"] if item["email"] else False,
                        func.lower(GtmContact.linkedin_url).in_((item["linkedin_url"].lower(), item["linkedin_url"].lower().rstrip("/"))) if item["linkedin_url"] else False))))
                duplicate = matches[0] if matches else None
                if duplicate and _local_importable(duplicate, vid) and (len(matches) > 1
                        or item["email"] and duplicate.email and item["email"] != duplicate.email.lower()
                        or item["linkedin_url"] and duplicate.linkedin_url
                        and item["linkedin_url"].lower().rstrip("/") != duplicate.linkedin_url.lower().rstrip("/")):
                    raise ContactImportError("These email and LinkedIn details match different people. Open Contacts > Import and correct the conflicting details before importing again.", 409)
                if (duplicate and duplicate.contact_provenance == "founder_import"
                        and duplicate.source == f"venture:{vid}:founder_import"
                        and duplicate.lawful_basis in {"consent", "legitimate_interest"}
                        and journey._json(duplicate.enrichment_json).get("founder_import", {}).get("lawful_basis_attested") is True):
                    _apply_history(duplicate, item["status"], item["last_contacted"], merge=True)
                    evidence = journey._json(duplicate.enrichment_json)
                    imported = evidence["founder_import"]
                    statuses = {"new": 0, "contacted": 1, "replied": 2, "interested": 3}
                    if statuses[item["status"]] >= statuses.get(imported.get("status"), 0):
                        imported["status"] = item["status"]
                    if duplicate.last_emailed_at:
                        imported["last_contacted"] = duplicate.last_emailed_at.isoformat()
                    duplicate.enrichment_json = json.dumps(evidence)
                    if duplicate.id not in result["contact_ids"]:
                        result["contact_ids"].append(duplicate.id)
                    result["skipped"] += 1
                    continue
                if item["duplicate"] and not duplicate:
                    result["skipped"] += 1
                    continue
                if duplicate and not _local_importable(duplicate, vid):
                    result["skipped"] += 1
                    continue
                name = item["name"].split(maxsplit=1)
                contact = duplicate or _persist_lead(session, wid, item | {"first_name": name[0] if name else None, "last_name": name[1] if len(name) > 1 else None,
                    "source": f"venture:{vid}:founder_import", "email_confidence": "risky" if item["email_check"] == "mx" else "guess" if item["email"] else "none"}, schedule_research=False)
                now = utcnow()
                contact.source = f"venture:{vid}:founder_import"
                contact.email_confidence = "risky" if item["email_check"] == "mx" else "guess" if item["email"] else "none"
                contact.contact_provenance = "founder_import"
                contact.lawful_basis, contact.lawful_basis_at = lawful_basis, now
                contact.lawful_basis_note = lawful_basis_note.strip() or "Founder attested a lawful basis when importing this contact."
                contact.recipient_geo = item["country"] or contact.recipient_geo
                evidence = journey._json(contact.enrichment_json)
                contact.enrichment_json = json.dumps(evidence | {"business": item["company"] or evidence.get("business"), "why": evidence.get("why") or "Founder imported contact; fit and prior outreach are founder reported.",
                    "founder_import": {"preview_id": preview_id, "row_number": item["row_number"], "imported_at": now.isoformat(), "attested_by": actor,
                        "lawful_basis_attested": True, "status": item["status"], "last_contacted": item["last_contacted"], "notes": item["notes"], "email_check": item["email_check"]}})
                _apply_history(contact, item["status"], item["last_contacted"], merge=bool(duplicate))
                result["contact_ids"].append(contact.id)
                result["imported"] += 1
            data["committed"] = result
            row.provenance_json, row.updated_at = json.dumps(data), utcnow()
            saved = journey._row(session, vid, create=True)
            saved_data = journey._json(saved.provenance_json)
            saved_data.update(state="ready", phase="prospects", message="Your contacts are saved. You can ask for an email to someone you choose." if result["contact_ids"]
                else "No contacts were imported. Open Contacts > Import, review the errors and duplicates in your preview, then preview your corrected contacts and import again.")
            journey._store(session, saved, saved_data)
            session.commit()
    result["drafts_status"] = "not_requested"
    result["message"] = ("Your contacts are saved. You can ask for an email to someone you choose." if result["contact_ids"]
        else "No contacts were imported. Open Contacts > Import, review the errors and duplicates in your preview, then preview your corrected contacts and import again.")
    from app.ventures.stage import get_stage, reassess
    reassess(vid)
    return result | {"founder_stage": get_stage(vid), "journey": journey.status(wid, vid, authorize=False)}


def update_history(wid: str, vid: str, contact_id: str, *, actor: str, status: str, last_contacted: str | None) -> dict:
    authority(wid, vid, write=True, actor=actor)
    if status not in {"new", "contacted", "replied", "interested"}:
        raise ContactImportError("Choose new, contacted, replied, or interested.")
    date = _date((last_contacted or "").strip())
    from app.autopilot.send_admission import try_send_admission
    with try_send_admission(vid) as admitted, db()() as session:
        if not admitted:
            raise ContactImportError("Wait for the current outreach send to finish, then update the contact.", 409)
        serialize_writes(session, key="founder-contact-import:" + wid)
        contact = session.get(GtmContact, contact_id, with_for_update=True)
        if (not contact or contact.workspace_id != wid or contact.source != f"venture:{vid}:founder_import"
                or contact.contact_provenance != "founder_import"):
            raise ContactImportError("This imported contact is unavailable.", 404)
        if journey._processing_block(session, wid):
            raise ContactImportError("Review the workspace outreach policy before updating contacts.", 409)
        data = journey._json(contact.enrichment_json)
        imported = data.get("founder_import") or {}
        if not imported.get("lawful_basis_attested"):
            raise ContactImportError("This imported contact is unavailable.", 404)
        changed = (imported.get("status"), imported.get("last_contacted")) != (status, date)
        imported.update(status=status, last_contacted=date, history_updated_at=utcnow().isoformat(), history_updated_by=actor)
        data["founder_import"] = imported
        contact.enrichment_json = json.dumps(data)
        real_history = session.scalar(select(GtmTouch.id).where(GtmTouch.workspace_id == wid, GtmTouch.venture_id == vid,
            GtmTouch.contact_id == contact_id, GtmTouch.simulated == 0).limit(1))
        # Imported annotations cannot undo a recorded send, reply, opt-out or bounce.
        if not real_history and not (contact.do_not_contact or contact.unsubscribed_at or contact.bounced_at):
            _apply_history(contact, status, date)
        if changed:
            for draft in session.scalars(select(VentureDocument).where(VentureDocument.venture_id == vid, VentureDocument.kind == "outreach_draft")):
                saved = journey._json(draft.provenance_json)
                if saved.get("contact_id") == contact_id and saved.get("channel") == "manual" and saved.get("state") in {"draft", "skipped"}:
                    saved.update(state="review_abandoned", reason="founder_updated_history")
                    draft.provenance_json, draft.updated_at = json.dumps(saved), utcnow()
        session.commit()
    drafts_status = _prepare_drafts(wid, vid)
    from app.ventures.stage import get_stage, reassess
    reassess(vid)
    return {"journey": journey.status(wid, vid, authorize=False), "founder_stage": get_stage(vid), "drafts_status": drafts_status,
            "message": "Your contact history was updated. Review the contact or ask for an email draft to continue."}


async def verify_email(wid: str, vid: str, contact_id: str, *, actor: str) -> dict:
    authority(wid, vid, write=True, actor=actor)
    from app.autopilot.send_admission import try_send_admission
    with try_send_admission(vid) as admitted:
        if not admitted:
            raise ContactImportError("Wait for the current outreach send to finish, then check the email.", 409)
        with db()() as session:
            contact = session.get(GtmContact, contact_id)
            if (not contact or contact.workspace_id != wid or contact.source != f"venture:{vid}:founder_import"
                    or contact.contact_provenance != "founder_import" or not contact.email):
                raise ContactImportError("This imported contact is unavailable.", 404)
            if journey._processing_block(session, wid):
                raise ContactImportError("Review the workspace outreach policy before checking email.", 409)
            if (journey._json(contact.enrichment_json).get("founder_import") or {}).get("lawful_basis_attested") is not True:
                raise ContactImportError("This imported contact is unavailable.", 404)
            expected = contact.email
            _email(expected)
        checks = await check_domains({expected.rsplit("@", 1)[1]})
        check = checks.get(expected.rsplit("@", 1)[1], "unknown")
        authority(wid, vid, write=True, actor=actor)
        with db()() as session:
            serialize_writes(session, key="founder-contact-import:" + wid)
            contact = session.get(GtmContact, contact_id, with_for_update=True)
            if (not contact or contact.workspace_id != wid or contact.source != f"venture:{vid}:founder_import"
                    or contact.contact_provenance != "founder_import" or contact.email != expected):
                raise ContactImportError("This contact changed. Refresh it before checking email again.", 409)
            if journey._processing_block(session, wid):
                raise ContactImportError("Review the workspace outreach policy before checking email.", 409)
            data = journey._json(contact.enrichment_json)
            imported = data.get("founder_import") or {}
            if imported.get("lawful_basis_attested") is not True:
                raise ContactImportError("This imported contact is unavailable.", 404)
            imported.update(email_check=check, email_checked_at=utcnow().isoformat())
            contact.enrichment_json = json.dumps(data)
            contact.email_confidence = "risky" if check == "mx" else "guess"
            session.commit()
    drafts_status = await asyncio.to_thread(_prepare_drafts, wid, vid)
    from app.ventures.stage import get_stage
    message = {"mx": "The domain has a mail service. Review the contact and any saved drafts before sending.",
               "missing_mx": "This email domain has no mail service. Check the address before contacting this person.",
               "unknown": "The email check is unavailable. Nothing will be sent; you can check again."}[check]
    return {"email_check": check, "message": message, "drafts_status": drafts_status,
            "journey": journey.status(wid, vid, authorize=False), "founder_stage": get_stage(vid)}
