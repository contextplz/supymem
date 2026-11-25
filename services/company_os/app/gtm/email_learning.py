"""Shared aggregates contain no names, drafts, raw addresses or workspace IDs."""
import hashlib
import html
import hmac
import json
import logging
import re
from datetime import timedelta, timezone
from secrets import token_hex
from urllib.parse import unquote

from sqlalchemy import func, select

from app.config import settings
from app.core.local_mode import is_local
from app.db.models import GtmContact
from app.db.session import serialize_writes
from app.gtm.shared_models import SharedAddressValidity, SharedEmailOutcome, SharedEmailPattern, PublicResearchCache
from app.state import db
from core.clock import utcnow

log = logging.getLogger(__name__)
_secret_error_logged = False
_diagnostic_key = token_hex(32)


def opaque(value: str, purpose: str = "address") -> str:
    secret = settings.company_os_vault_secret
    if is_local():
        from app.connectors.secrets import vault_secret

        try:
            secret = vault_secret()
        except (OSError, ValueError):
            if purpose not in {"search-diagnostics", "source-yield"}:
                raise
            # shortcut: these diagnostic IDs are process-local when the vault is unavailable; use an owner-scoped key if restart correlation is needed.
            secret = _diagnostic_key
    if settings.app_env != "local" and (len(secret) < 32 or secret == "company-os-local-vault"):
        raise ValueError("gtm_shared_secret_unavailable")
    # Derived per-purpose key: the vault secret itself is never used directly as an HMAC key.
    key = hmac.new(secret.encode(), ("gtm-shared:" + purpose).encode(), hashlib.sha256).digest()
    return hmac.new(key, value.strip().casefold().encode(), hashlib.sha256).hexdigest()


def _shared_available():
    global _secret_error_logged
    if is_local():
        return False
    try:
        opaque("")
    except ValueError as exc:
        if str(exc) != "gtm_shared_secret_unavailable":
            raise
        if not _secret_error_logged:
            _secret_error_logged = True
            log.error("gtm_shared_secret_unavailable: shared email learning disabled; workspace suppression remains active")
        return False
    return True


def _aware(date):
    return date.replace(tzinfo=timezone.utc) if date.tzinfo is None else date.astimezone(timezone.utc)


def _active_retirement(row, now):
    return bool(row and row.retired_at and now < _aware(row.retired_at) + timedelta(days=90))


def _summary(events, now):
    # ponytail: per-domain history scan; pre-aggregate by day if outcome volume grows.
    kinds = {kind: [] for kind in ("send", "delivery", "reply", "hard_bounce", "soft_bounce")}
    for event in events:
        kind = event.kind
        if kind == "send" and now >= _aware(event.observed_at) + timedelta(hours=72):
            bounced = any(other.address_hash == event.address_hash and other.workspace_hash == event.workspace_hash
                and other.kind in {"hard_bounce", "soft_bounce"}
                and _aware(event.observed_at) <= _aware(other.observed_at) <= _aware(event.observed_at) + timedelta(hours=72) for other in events)
            if not bounced:
                kinds["delivery"].append(event)
        if kind in kinds:
            kinds[kind].append(event)
    values = {kind: sum(2 ** (-max(0, (now - _aware(event.observed_at)).total_seconds()) / (90 * 86400)) for event in rows) for kind, rows in kinds.items()}
    workspaces = {kind: {event.workspace_hash for event in rows} for kind, rows in kinds.items()}
    return values, workspaces


def refresh(session, address_hash, domain, pattern=None, now=None):
    if is_local():
        return None
    now = _aware(now or utcnow())
    events = list(session.scalars(select(SharedEmailOutcome).where(SharedEmailOutcome.address_hash == address_hash)))
    row = session.get(SharedAddressValidity, address_hash)
    if row is None:
        row = SharedAddressValidity(address_hash=address_hash, status="unknown", suppressed=False, last_seen=now)
        session.add(row)
    values, workspaces = _summary(events, now)
    row.workspace_count = len({event.workspace_hash for event in events})
    row.delivery_workspaces, row.reply_workspaces, row.hard_bounce_workspaces = [len(workspaces[kind]) for kind in ("delivery", "reply", "hard_bounce")]
    row.deliveries, row.replies, row.hard_bounces = [values[kind] for kind in ("delivery", "reply", "hard_bounce")]
    expired = bool(row.retired_at and not _active_retirement(row, now))
    recent_bounce = any(event.kind == "hard_bounce" and _aware(event.observed_at) > _aware(row.retired_at) for event in events) if row.retired_at else True
    retirement_events = [event for event in events if not expired or _aware(event.observed_at) >= _aware(row.retired_at) + timedelta(days=90)]
    retirement_values, retirement_workspaces = _summary(retirement_events, now)
    hard_workspaces = len(retirement_workspaces["hard_bounce"])
    retire = hard_workspaces >= 3 and retirement_values["hard_bounce"] > row.deliveries or hard_workspaces >= 2 and not row.deliveries
    if retire and not _active_retirement(row, now) and (not expired or recent_bounce):
        row.retired_at = now
    fresh_success = bool(retirement_values["delivery"] or retirement_values["reply"]) if expired else False
    row.status = "retired" if _active_retirement(row, now) else "unknown" if expired and not fresh_success else "deliverable" if row.deliveries or row.replies else "unknown"
    row.last_seen = max((_aware(event.observed_at) for event in events), default=now)
    if pattern:
        events = list(session.scalars(select(SharedEmailOutcome).where(SharedEmailOutcome.domain == domain, SharedEmailOutcome.pattern == pattern)))
        item = session.get(SharedEmailPattern, (domain, pattern))
        if item is None:
            item = SharedEmailPattern(domain=domain, pattern=pattern, last_seen=now)
            session.add(item)
        values, workspaces = _summary(events, now)
        item.workspace_count = len({event.workspace_hash for event in events if event.kind not in {"pattern", "guess"}})
        item.sends, item.deliveries, item.replies, item.hard_bounces = [values[kind] for kind in ("send", "delivery", "reply", "hard_bounce")]
        expired = bool(item.retired_at and not _active_retirement(item, now))
        new_bounce = any(event.kind == "hard_bounce" and _aware(event.observed_at) > _aware(item.retired_at) for event in events) if item.retired_at else True
        retirement_events = [event for event in events if not expired or _aware(event.observed_at) >= _aware(item.retired_at) + timedelta(days=90)]
        retirement_values, retirement_workspaces = _summary(retirement_events, now)
        if len({event.workspace_hash for event in retirement_events if event.kind not in {"pattern", "guess"}}) >= 3 and len(retirement_workspaces["hard_bounce"]) >= 2 and sum(event.kind == "send" for event in retirement_events) >= 5 and retirement_values["send"] and retirement_values["hard_bounce"] / retirement_values["send"] > .5 and not _active_retirement(item, now) and (not expired or new_bounce):
            item.retired_at = now
        item.published_pattern = bool(item.published_pattern or any(event.kind == "pattern" for event in events))
        guessed = not item.published_pattern
        item.confidence = max(10, min(95, round((40 if guessed else 80) + item.replies * 5 - item.hard_bounces * 15 - values["soft_bounce"] * 5)))
        if expired:
            item.confidence = min(30, item.confidence)
        item.last_seen = max((_aware(event.observed_at) for event in events), default=now)
    session.flush()
    return row


def record(session, contact, kind, event_id, *, pattern=None, observed_at=None):
    if not contact.email:
        return
    from app.lead_library.service import on_outcome
    on_outcome(session, contact, kind, observed_at)
    if not _shared_available():
        return
    now = observed_at or utcnow()
    address_hash = opaque(contact.email)
    event_key = opaque(contact.workspace_id + ":" + str(event_id) + ":" + kind, "event")
    if session.get_bind().dialect.name != "sqlite" or not session.connection().connection.driver_connection.in_transaction:
        serialize_writes(session, key="gtm-shared-outcomes")
    from app.gtm.journey import _json
    data = _json(contact.enrichment_json)
    route = data.get("email_route") or {}
    pattern = pattern or route.get("pattern")
    if not pattern:
        from app.gtm.enrich import email_patterns
        name = data.get("person_name") or " ".join(part for part in (contact.first_name, contact.last_name) if part)
        pattern = next((key for key, local in email_patterns(name).items() if local == contact.email.split("@")[0].lower()), None)
        if pattern and data.get("email_evidence") == "published":
            route = {"route_type": "pattern"}
    domain = contact.email.rsplit("@", 1)[-1].casefold()
    if session.get(SharedEmailOutcome, event_key) is None:
        if route.get("route_type") in {"guess", "pattern"}:
            origin_key = opaque(contact.workspace_id + ":" + address_hash + ":origin", "event")
            if session.get(SharedEmailOutcome, origin_key) is None:
                session.add(SharedEmailOutcome(event_key=origin_key, address_hash=address_hash,
                    workspace_hash=opaque(contact.workspace_id, "workspace"), domain=domain, pattern=pattern,
                    kind=route["route_type"], observed_at=now))
        session.add(SharedEmailOutcome(event_key=event_key, address_hash=address_hash, workspace_hash=opaque(contact.workspace_id, "workspace"),
            domain=domain, pattern=pattern, kind=kind, observed_at=now))
        session.flush()
    refresh(session, address_hash, domain, pattern)


def suppress(session, email):
    if not email:
        return
    from app.lead_library.service import on_suppress
    on_suppress(session, email)
    if not _shared_available():
        return
    from app.gtm.relationships.suppression import _mailbox_email
    address_hash = opaque(_mailbox_email(email))
    if session.get_bind().dialect.name != "sqlite" or not session.connection().connection.driver_connection.in_transaction:
        serialize_writes(session, key="gtm-shared-outcomes")
    row = refresh(session, address_hash, email.rsplit("@", 1)[-1])
    row.suppressed = True
    session.info.pop("gtm_shared_suppression", None)


def globally_suppressed(session, email):
    if not email or not _shared_available():
        return False
    if session is None:
        with db()() as owned:
            return globally_suppressed(owned, email)
    from app.gtm.relationships.suppression import _mailbox_email
    saved = session.info.get("gtm_shared_suppression", {})
    for address in dict.fromkeys((email, _mailbox_email(email))):
        address_hash = opaque(address)
        if address_hash in saved:
            if saved[address_hash]:
                return True
            continue
        row = session.get(SharedAddressValidity, address_hash)
        if row and (row.suppressed or _active_retirement(row, _aware(utcnow()))):
            return True
    return False


def prefetch_suppression(session, emails, *, workspace_id=None, generated_emails=()):
    generated = {email.strip().casefold() for email in generated_emails if email}
    blocked = set(session.scalars(select(func.lower(GtmContact.email)).where(GtmContact.workspace_id == workspace_id,
        func.lower(GtmContact.email).in_(generated), GtmContact.bounced_at.is_not(None)))) if generated and workspace_id else set()
    session.info["gtm_generated_blocked"] = blocked
    session.info["gtm_retired_patterns"] = {}
    session.info["gtm_shared_opt_out"] = set()
    if not _shared_available():
        session.info.pop("gtm_shared_suppression", None)
        return
    hashes = {opaque(email) for email in emails if email}
    rows = list(session.scalars(select(SharedAddressValidity).where(SharedAddressValidity.address_hash.in_(hashes)))) if hashes else []
    saved = dict.fromkeys(hashes, False)
    saved.update({row.address_hash: bool(row.suppressed or _active_retirement(row, _aware(utcnow()))) for row in rows})
    session.info["gtm_shared_suppression"] = saved
    session.info["gtm_shared_opt_out"] = {row.address_hash for row in rows if row.suppressed}
    if generated:
        addresses = {opaque(email): email for email in generated}
        if workspace_id:
            bounced = session.scalars(select(SharedEmailOutcome.address_hash).where(
                SharedEmailOutcome.workspace_hash == opaque(workspace_id, "workspace"),
                SharedEmailOutcome.address_hash.in_(addresses), SharedEmailOutcome.kind.in_(("hard_bounce", "soft_bounce"))))
            blocked.update(addresses[address_hash] for address_hash in bounced)
        domains = {email.rsplit("@", 1)[-1] for email in generated}
        retired = {domain: set() for domain in domains}
        for row in session.scalars(select(SharedEmailPattern).where(SharedEmailPattern.domain.in_(domains))):
            if _active_retirement(row, _aware(utcnow())):
                retired[row.domain].add(row.pattern)
        session.info["gtm_retired_patterns"] = retired


def candidate(email, wid, pattern=None):
    available = _shared_available()
    with db()() as session:
        if available:
            serialize_writes(session, key="gtm-shared-outcomes")
        local = session.scalar(select(GtmContact.id).where(GtmContact.workspace_id == wid,
            GtmContact.email == email, GtmContact.bounced_at.is_not(None)))
        if not available:
            return {"blocked": bool(local), "confirmed": False, "expired": False}
        row = refresh(session, opaque(email), email.rsplit("@", 1)[-1], pattern)
        events = list(session.scalars(select(SharedEmailOutcome).where(SharedEmailOutcome.address_hash == row.address_hash)))
        _, workspaces = _summary(events, _aware(utcnow()))
        other = (workspaces["delivery"] | workspaces["reply"]) - {opaque(wid, "workspace")}
        local_bounce = any(event.workspace_hash == opaque(wid, "workspace") and event.kind in {"hard_bounce", "soft_bounce"} for event in events)
        session.commit()
        return {"blocked": bool(local or local_bounce or row.suppressed or row.status == "retired"),
            "confirmed": bool(other and row.status == "deliverable"), "expired": bool(row.retired_at and row.status == "unknown")}


def learned_patterns(domain):
    if not _shared_available():
        return []
    with db()() as session:
        serialize_writes(session, key="gtm-shared-outcomes")
        rows = list(session.scalars(select(SharedEmailPattern).where(SharedEmailPattern.domain == domain)))
        for row in rows:
            event = session.scalar(select(SharedEmailOutcome).where(SharedEmailOutcome.domain == domain, SharedEmailOutcome.pattern == row.pattern).limit(1))
            if event:
                refresh(session, event.address_hash, domain, row.pattern)
            elif row.retired_at and not _active_retirement(row, _aware(utcnow())):
                row.confidence = min(row.confidence, 30)
        result = [(row.pattern, row.confidence, row.published_pattern) for row in rows if not _active_retirement(row, _aware(utcnow()))]
        session.commit()
        return sorted(result, key=lambda item: -item[1])


def retired_patterns(domain, *, session=None):
    if is_local():
        return set()
    if session is not None and domain in session.info.get("gtm_retired_patterns", {}):
        return session.info["gtm_retired_patterns"][domain]
    if not _shared_available():
        return set()
    with db()() as session:
        return {row.pattern for row in session.scalars(select(SharedEmailPattern).where(SharedEmailPattern.domain == domain))
            if _active_retirement(row, _aware(utcnow()))}


def cache_key(provider, operation, identity):
    parts = [provider, operation, identity]
    if is_local():
        parts.insert(0, "owner_local")
    return hashlib.sha256(json.dumps(parts, sort_keys=True).encode()).hexdigest()


def _has_contact_addresses(response):
    from app.gtm.enrich import _published_contacts
    if isinstance(response, str):
        text = html.unescape(unquote(response))
        return bool(re.search(r"@(?:[\w-]+\.)+[\w-]{2,}\b", text) or _published_contacts(text)[0])
    if isinstance(response, dict):
        response = [*response, *response.values()]
    return isinstance(response, (list, tuple)) and any(_has_contact_addresses(item) for item in response)


def cached(key):
    with db()() as session:
        row = session.get(PublicResearchCache, key)
        value = json.loads(row.response_json) if row and _aware(row.saved_at) > _aware(utcnow()) - timedelta(days=7) else None
        if _has_contact_addresses(value):
            return None
        if isinstance(value, dict) and value.get("cache_type") == "scraped_text":
            from app.signals.research import ScrapedText
            return ScrapedText(value["text"], metadata=value["metadata"], usage={})
        return value


def cache_save(key, response):
    if response is None or response == "":
        return
    if isinstance(response, str) and getattr(response, "metadata", None):
        response = {"cache_type": "scraped_text", "text": str(response), "metadata": response.metadata}
    if _has_contact_addresses(response):
        return
    with db()() as session:
        serialize_writes(session, key="gtm-public-cache:" + key)
        row = session.get(PublicResearchCache, key)
        if row is None:
            row = PublicResearchCache(cache_key=key, response_json="null", saved_at=utcnow())
            session.add(row)
        row.response_json, row.saved_at = json.dumps(response), utcnow()
        session.commit()
