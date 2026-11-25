"""Shared founder-mail compliance, also used to build the exact review text."""
from __future__ import annotations

import os
import re

from app.db.models import GtmOutreachSettings
from app.core.local_mode import is_local
from core.copy import no_dashes  # noqa: F401  (shared, re-exported)
from app.gtm.relationships.geo_policy import evaluate_legal_send, normalize_geo
from app.lead_library.taxonomy import EU_UK

OPT_OUT = 'If this is not relevant, just reply "no thanks" and I will not email you again.'
NO_DASH_RULE = "Never use em dashes or en dashes; use a comma or a full stop instead."
ADDRESS_PROMPT = "Add your business mailing address in Find customers → Sending address for your footer, then click Save sending address. Your drafts stay saved."
_SOURCE_LINE = re.compile(
    r"(?m)^(?:Your email address is listed publicly on \S+\.|I have your address from my own contacts\.|"
    r"Your work email is listed in a business contact directory\.)[ \t]*(?:\r?\n|$)")


def sender_address(session, workspace_id: str) -> str:
    from app.core.local_mode import is_sample_mode, is_test_mode
    sample = is_sample_mode()
    if is_local():
        from app.core.local_secrets import read_settings
        value = read_settings("smtp")["values"].get("postal_address", "")
        if not isinstance(value, str):
            raise ValueError("Open Keys and connections > Email and enter your business postal address, then try again.")
        if value.strip() or not sample:
            return value.strip()
    if sample or is_test_mode():
        return "Sample sender address, not for sending"
    settings = session.get(GtmOutreachSettings, workspace_id)
    return ((settings.sender_postal_address or "").strip() if settings else "") or os.environ.get("EMAIL_PHYSICAL_ADDRESS", "").strip()


def source_line(contact) -> str:
    from app.gtm.journey import _json
    evidence = _json(contact.enrichment_json) if contact else {}
    if (evidence.get("email_route") or {}).get("route_type") in {"guess", "pattern"}:
        # ponytail: no approved guessed-address disclosure; keep these review-only until one exists.
        return ""
    source = evidence.get("contact_source_url") or evidence.get("found_at") or evidence.get("source_url")
    # Source disclosure is required on every cold email; written as a plain sentence, not an internal label.
    if evidence.get("founder_import"):
        return "I have your address from my own contacts."
    library = evidence.get("lead_library")
    if isinstance(library, dict) and not library.get("email_published"):
        return "Your work email is listed in a business contact directory."
    return source_disclosure(source) if source else ""


def source_disclosure(url: str) -> str:
    """The recipient-facing sentence saying where their address was found (required on cold email)."""
    from urllib.parse import urlparse
    host = (urlparse(str(url)).hostname or str(url)).removeprefix("www.")
    return f"Your email address is listed publicly on {host}."


def consent_block(contact) -> str | None:
    from app.gtm.journey import _json
    evidence = _json(contact.enrichment_json)
    hint = evidence.get("outreach_hint") if isinstance(evidence.get("outreach_hint"), dict) else {}
    subscriber = str(hint.get("subscriber") or "").lower().replace("-", "_").replace(" ", "_")
    countries = {normalize_geo(contact.recipient_geo), normalize_geo(hint.get("country"))}
    restricted = subscriber in {"consumer", "individual", "unverified_individual", "sole_trader", "sole_trader_or_partnership", "ordinary_partnership", "partnership"}
    if restricted and (countries & {"GB", "UK"} or subscriber == "consumer"):
        if not (contact.lawful_basis == "consent" and contact.lawful_basis_at and (contact.lawful_basis_note or "").strip()):
            return "gtm_recipient_consent_required"
    if contact.contact_provenance == "lead_library" and countries & (EU_UK | {"UK"}):
        # A licensed list is not permission for EU/UK recipients; the founder records it per contact.
        if not (contact.lawful_basis_at and (contact.lawful_basis_note or "").strip()):
            return "gtm_recipient_consent_required"
    return None


def legacy_cleanup(body: str) -> str:
    """Drafts saved before the current format: no "Source:"/"Product:" label lines, capitalized team signature."""
    if re.search(r"(?m)^(?:Source:|Product:)", body or ""):
        body = re.sub(r"\bI found [^.!?\n]+[.!]\s*", "", body)
    body = re.sub(r"(?m)^Source:[^\n]*(?:\n|$)", "", body or "")  # the disclosure sentence replaces them
    body = re.sub(r"(?m)^Product:[ \t]*(\S[^\n]*)$", r"You can see what I'm building here: \1", body)
    body = re.sub(r"(?m)^the (\S[^\n]*) team$", r"The \1 team", body)
    return re.sub(r"\n{3,}", "\n\n", body).strip()


def complete_body(session, workspace_id: str, contact, body: str, identity: str) -> str:
    from app.db.models import Person, Workspace
    from app.gtm.relationships.unsubscribe import unsubscribe_url

    if is_local():
        from app.gtm.local_mail import _values
        values = _values()
        identity, sender = values["SENDER_NAME"], values["SMTP_USERNAME"]
        if any(not isinstance(value, str) or "\r" in value or "\n" in value for value in (identity, sender)):
            raise ValueError("Keep your sender name and mailbox address on one line, then try again.")
    workspace = session.get(Workspace, workspace_id)
    owner = session.get(Person, workspace.owner_id) if workspace and workspace.owner_id else None
    full_name = (owner.display_name or "").strip() if owner else ""
    signatures = {identity.strip()}
    if identity.strip() and full_name.startswith(identity.strip() + " ") and not is_local():
        signatures.add(full_name)
    source = source_line(contact)
    url = unsubscribe_url(workspace_id, contact.id, contact.email) if contact else ""
    opt_out = "Stop these emails: " + url if url else ""
    if is_local() and not url:
        from app.gtm.relationships.unsubscribe import REPLY_OPT_OUT
        opt_out = REPLY_OPT_OUT
    body = legacy_cleanup(_SOURCE_LINE.sub(
        lambda match: match[0] if match[0].strip() == source else "", body or ""))
    body = re.sub(r"(?m)^Stop these emails: \S+[ \t]*(?:\n|$)",
        lambda match: match[0] if match[0].strip() == opt_out else "", body)
    parts = [body]
    for line in (source, identity, OPT_OUT, opt_out):
        present = (any(signature.strip() in signatures for signature in body.splitlines())
                   if line == identity else line in body)
        if line and not present:
            parts.append(line)
    if is_local() and sender and sender not in body:
        parts.append(f"From: {sender}")
    address = sender_address(session, workspace_id)
    if address and address not in body:
        parts.append(f"Business outreach · {address}")
    return "\n\n".join(parts)


def with_opt_out_link(contact, body: str) -> str:
    """Drafts saved before the signed link existed gain it when read, so they can still be sent."""
    from app.gtm.relationships.unsubscribe import unsubscribe_url

    url = unsubscribe_url(contact.workspace_id, contact.id, contact.email)
    if is_local() and not url:
        return body
    line = "Stop these emails: " + url
    return body if line in body else f"{body}\n\n{line}"


def complete_review_text(venture_id: str, text: str, *, request: str = "") -> str:
    from app.autopilot.outreach import founder_signature
    from app.connections import mailboxes
    from app.db.models import GtmContact, Venture
    from app.state import db
    from sqlalchemy import func, select

    with db()() as session:
        venture = session.get(Venture, venture_id)
        if venture is None:
            raise ValueError("Open your startup and ask for drafts again.")
        try:
            sender = mailboxes.resolve_send_mailbox(session, venture.workspace_id, venture_id=venture_id)["email"]
        except mailboxes.MailboxUnavailable:
            sender = ""
        emails = {email.lower() for email in re.findall(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", request)}
        contact = session.scalar(select(GtmContact).where(
            GtmContact.workspace_id == venture.workspace_id,
            GtmContact.source.like(f"venture:{venture_id}:%"),
            func.lower(GtmContact.email) == next(iter(emails)))) if len(emails) == 1 else None
        identity = founder_signature(venture_id)
        parts = re.split(r"(?im)(?=^Draft\s+\d+\b)", text)
        for index, part in enumerate(parts):
            if re.search(r"(?im)^Subject:", part):
                part = complete_body(session, venture.workspace_id, contact, part.strip(), identity)
                parts[index] = complete_body(session, venture.workspace_id, contact, part, sender)
        return "\n\n".join(part.strip() for part in parts if part.strip())


def validate_send(session, workspace_id: str, contact, body: str, identity: str, *, draft: dict | None = None) -> None:
    from app.autopilot import claims
    from app.gtm.relationships.unsubscribe import unsubscribe_url

    if is_local():
        from app.gtm.local_mail import settings
        identity = settings().sender_name

    if draft and claims.generated_email(draft):
        venture_id = contact.source.split(":", 2)[1] if contact and (contact.source or "").startswith("venture:") else None
        if claims.violations(body, founder_words=claims.founder_words(venture_id) if venture_id else ""):
            raise ValueError("gtm_draft_unsupported_claim")

    if contact:
        from app.gtm.enrich import generated_route_allowed
        from app.gtm.journey import _json
        evidence = _json(contact.enrichment_json)
        if evidence.get("email_route") and not generated_route_allowed(evidence, contact.email, contact.recipient_geo, for_send=True):
            raise ValueError("gtm_generated_email_review_required")
    address = sender_address(session, workspace_id)
    if not address:
        raise ValueError("gtm_sender_address_required")
    if address not in body:
        raise ValueError("gtm_sender_address_changed")
    if not identity or identity not in body:
        raise ValueError("gtm_sender_identity_required")
    if is_local() and identity not in body.splitlines():
        raise ValueError("Add your sender name on its own line in your email footer, then try again.")
    if OPT_OUT not in body:
        raise ValueError("gtm_draft_one_ask_and_optout_required")
    source = source_line(contact)
    if (not source or source not in body or re.search(r"(?m)^Source:", body)
            or any(match[0].strip() != source for match in _SOURCE_LINE.finditer(body))):
        raise ValueError("gtm_source_disclosure_required")
    if is_local() and not unsubscribe_url(workspace_id, contact.id, contact.email) and re.search(r"(?m)^Stop these emails:", body):
        raise ValueError("Remove the old opt-out link and keep your reply-based opt-out line, then try again.")
    if contact and unsubscribe_url(workspace_id, contact.id, contact.email) not in body:
        raise ValueError("gtm_draft_one_ask_and_optout_required")
    if contact:
        blocked = consent_block(contact)
        decision = evaluate_legal_send(country=contact.recipient_geo, channel="email", lawful_basis=contact.lawful_basis,
            sender_postal_address=address, legitimate_use_note=contact.lawful_basis_note, send_mode="gmail_composio",
            mined_third_party=":public" in (contact.source or ""))
        if blocked or not decision.allowed:
            raise ValueError(blocked or decision.reason)
