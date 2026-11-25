"""Current recipient suppression, checked again immediately before email delivery."""

from __future__ import annotations

from sqlalchemy import func, or_, select

from app.db.models import GtmContact, GtmSuppression, Venture, VentureDocument, Workspace


def _mailbox_email(email: str | None) -> str:
    local, separator, domain = (email or "").strip().lower().partition("@")
    return local.partition("+")[0] + separator + domain


def _suppression_match(email: str | None):
    mailbox = _mailbox_email(email)
    local, separator, domain = mailbox.partition("@")
    saved = func.lower(func.trim(GtmSuppression.email))
    if not separator:
        return saved == mailbox
    return or_(saved == mailbox, saved.startswith(local + "+", autoescape=True) & saved.endswith("@" + domain, autoescape=True))


def opted_out(session, workspace_id: str, contact: GtmContact) -> bool:
    if contact.workspace_id != workspace_id:
        return False
    return bool(contact.unsubscribed_at or session.scalar(select(GtmSuppression.id).where(
        GtmSuppression.workspace_id == workspace_id,
        _suppression_match(contact.email),
        or_(GtmSuppression.source == "opt_out", GtmSuppression.reason.in_(("replied_opt_out", "public_unsubscribe", "opted_out")))
    ).limit(1)))


def email_suppressed(session, workspace_id: str, contact: GtmContact, *, replied_ok: bool = False, suppressed_emails: set[str] | None = None) -> bool:
    """replied_ok: a founder-approved answer to the contact's own reply may go out; every other stop still applies."""
    if contact.workspace_id != workspace_id:
        return True
    owner = select(Workspace.owner_id).where(Workspace.id == workspace_id).scalar_subquery()
    workspaces = select(Workspace.id).where(Workspace.owner_id == owner)
    query = select(func.lower(GtmSuppression.email)).where(
        or_(GtmSuppression.workspace_id == workspace_id, GtmSuppression.workspace_id.in_(workspaces)))
    if suppressed_emails is not None:
        stops = session.info.setdefault("gtm_owner_suppression", {})
        if workspace_id not in stops:
            stops[workspace_id] = {_mailbox_email(email) for email in session.scalars(query)}
    if (contact.replied_at and not replied_ok) or contact.do_not_contact or contact.unsubscribed_at or contact.bounced_at:
        return True
    email = (contact.email or "").strip().lower()
    if not email:
        return True
    if email in session.info.get("gtm_generated_blocked", set()):
        return True
    from app.gtm.journey import _json
    data = _json(contact.enrichment_json)
    if data.get("skipped"):
        return True
    from app.core.local_mode import is_local
    if is_local() and not replied_ok:
        from app.gtm.contact_work import NOTE
        booked = session.info.setdefault("gtm_manual_booked_contacts", {}) if suppressed_emails is not None else {}
        if workspace_id not in booked:
            notes = session.scalars(select(VentureDocument).join(Venture, Venture.id == VentureDocument.venture_id)
                .where(Venture.workspace_id == workspace_id, VentureDocument.kind == NOTE))
            booked[workspace_id] = {
                _json(note.provenance_json).get("contact_id") for note in notes
                if _json(note.provenance_json).get("type") == "manual_outreach"
                and _json(note.body_md).get("booked_at")
            }
        if contact.id in booked[workspace_id]:
            return True
    if data.get("email_route"):
        from app.gtm.enrich import generated_route_allowed
        from app.gtm.email_learning import retired_patterns
        from app.gtm.journey import _consumer, _icp
        venture = (contact.source or "").split(":")[1] if (contact.source or "").startswith("venture:") else None
        consumers = session.info.setdefault("gtm_consumer_ventures", {})
        if venture and venture not in consumers:
            consumers[venture] = _consumer(_icp(venture))
        if (venture and consumers[venture]) or not generated_route_allowed(data, email, contact.recipient_geo) or data["email_route"].get("pattern") in retired_patterns(email.rsplit("@", 1)[-1], session=session):
            return True
    from app.gtm.email_learning import globally_suppressed
    if globally_suppressed(session, email):
        return True
    from app.lead_library.service import over_cap
    if over_cap(session, contact):
        return True
    if suppressed_emails is not None:
        mailbox = _mailbox_email(email)
        return mailbox in {_mailbox_email(value) for value in suppressed_emails} or mailbox in stops[workspace_id]
    return session.scalar(query.where(_suppression_match(email)).limit(1)) is not None
