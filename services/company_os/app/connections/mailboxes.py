"""Owned Gmail identities shared by sending, reading and connection surfaces."""

from __future__ import annotations

import json
from email.errors import HeaderParseError
from email.headerregistry import Address
from typing import TypedDict

from sqlalchemy import select

from app.core.local_mode import is_local
from app.credits.policy import get_policy
from app.db.models import GtmContact, GtmEmailAccount, GtmEmailEvent, GtmTouch, Venture, Workspace


class MailboxRef(TypedDict):
    mailbox_id: str
    email: str
    connected_account_id: str
    composio_user_id: str
    owner_workspace_id: str
    status: str


class MailboxUnavailable(ValueError):
    pass


def enabled() -> bool:
    flags = get_policy("feature_flags")
    return isinstance(flags, dict) and flags.get("multi_gmail_v1") is True


def _ref(account: GtmEmailAccount) -> MailboxRef:
    return MailboxRef(
        mailbox_id=account.id, email=account.email or "",
        connected_account_id=account.connected_account_id,
        composio_user_id=account.composio_user_id,
        owner_workspace_id=account.workspace_id, status=account.status,
    )


def _owned_account(session, workspace_id: str, mailbox_id: str) -> GtmEmailAccount:
    from app.gtm.supymem.pipeline import _gmail_workspace_ids

    if not isinstance(mailbox_id, str) or not mailbox_id:
        raise MailboxUnavailable("mailbox_unavailable")
    account = session.get(GtmEmailAccount, mailbox_id)
    if is_local():
        from app.gtm.local_mail import ensure_account
        local = ensure_account(session, workspace_id)
        if account is None or account.id != local.id:
            raise MailboxUnavailable("Select your current local mailbox, then try again.")
        return account
    if (session.get(Workspace, workspace_id) is None or account is None or account.provider != "gmail" or account.status != "active"
            or account.workspace_id not in _gmail_workspace_ids(session, workspace_id)
            or not account.connected_account_id or not account.composio_user_id):
        raise MailboxUnavailable("mailbox_unavailable")
    return account


def list_read_mailboxes(session, workspace_id: str) -> list[MailboxRef]:
    from app.gtm.supymem.pipeline import _active_mailbox, _mailboxes_query

    if is_local():
        from app.gtm.local_mail import settings
        account = _active_mailbox(session, workspace_id)
        return [_ref(account)] if account is not None and settings().imap_host else []
    if not enabled():
        account = _active_mailbox(session, workspace_id)
        return [_ref(account)] if account is not None else []
    if session.get(Workspace, workspace_id) is None:
        return []
    return [_ref(account) for account in session.scalars(
        _mailboxes_query(session, workspace_id).where(GtmEmailAccount.status == "active")
    ) if account.status == "active" and account.connected_account_id and account.composio_user_id]


def _venture(session, workspace_id: str, venture_id: str) -> Venture:
    venture = session.get(Venture, venture_id)
    if venture is None or venture.workspace_id != workspace_id or venture.archived_at is not None:
        raise MailboxUnavailable("venture_unavailable")
    return venture


def _workspace_default_mailbox_id(session, workspace_id: str) -> str | None:
    from app.gtm.supymem.pipeline import _legacy_active_mailbox, _mailboxes_query
    workspace = session.get(Workspace, workspace_id)
    if workspace is None:
        return None
    if workspace.default_gmail_mailbox_id is None:
        original = _legacy_active_mailbox(session, workspace_id) or session.scalar(_mailboxes_query(session, workspace_id).where(
            GtmEmailAccount.status == "needs_reconnect"))
        if original is not None:
            workspace.default_gmail_mailbox_id = original.id
    return workspace.default_gmail_mailbox_id


def resolve_send_mailbox(session, workspace_id: str, *, venture_id=None, mailbox_id=None) -> MailboxRef:
    from app.gtm.supymem.pipeline import _active_mailbox

    if is_local():
        from app.gtm.local_mail import ensure_account
        if venture_id is not None:
            _venture(session, workspace_id, venture_id)
        account = ensure_account(session, workspace_id)
        if mailbox_id is not None and mailbox_id != account.id:
            raise MailboxUnavailable("Select your current local mailbox, then try again.")
        return _ref(account)
    if not enabled():
        account = _active_mailbox(session, workspace_id)
        if account is None:
            raise MailboxUnavailable("mailbox_unavailable")
        return _ref(account)
    venture = _venture(session, workspace_id, venture_id) if venture_id is not None else None
    selected = mailbox_id
    if selected is None and venture is not None:
        selected = venture.default_gmail_mailbox_id
    if selected is None:
        selected = _workspace_default_mailbox_id(session, workspace_id)
    if selected is None:
        raise MailboxUnavailable("mailbox_unavailable")
    account = _owned_account(session, workspace_id, selected)
    if not account.email or len(account.email) > 320 or "\r" in account.email or "\n" in account.email:
        raise MailboxUnavailable("mailbox_unavailable")
    try:
        address = Address(addr_spec=account.email)
        if not address.username or not address.domain:
            raise ValueError("email_unavailable")
    except (ValueError, HeaderParseError) as exc:
        raise MailboxUnavailable("mailbox_unavailable") from exc
    return _ref(account)


def set_default_mailbox(session, workspace_id: str, mailbox_id: str | None, *, venture_id=None) -> None:
    if not enabled():
        raise MailboxUnavailable("multi_gmail_disabled")
    target = _venture(session, workspace_id, venture_id) if venture_id is not None else session.get(Workspace, workspace_id)
    if target is None:
        raise MailboxUnavailable("workspace_unavailable")
    if mailbox_id is None:
        if venture_id is None:
            raise MailboxUnavailable("mailbox_unavailable")
        target.default_gmail_mailbox_id = None
    else:
        target.default_gmail_mailbox_id = _owned_account(session, workspace_id, mailbox_id).id


def mailbox_defaults(workspace_id: str, *, venture_id=None) -> dict:
    from app.state import db

    with db()() as session:
        workspace = session.get(Workspace, workspace_id)
        if workspace is None:
            raise MailboxUnavailable("workspace_unavailable")
        result = {"workspace_default_mailbox_id": workspace.default_gmail_mailbox_id}
        if venture_id is not None:
            result["venture_default_mailbox_id"] = _venture(session, workspace_id, venture_id).default_gmail_mailbox_id
        return result


def save_default_mailbox(workspace_id: str, mailbox_id: str | None, *, venture_id=None) -> None:
    from app.state import db

    with db()() as session:
        set_default_mailbox(session, workspace_id, mailbox_id, venture_id=venture_id)
        session.commit()


def mailbox_for_conversation(session, *, contact_id=None, thread_id=None) -> MailboxRef | None:
    if contact_id is None and thread_id is None:
        return None
    contacts = session.scalars(select(GtmContact).where(
        GtmContact.id == contact_id if contact_id is not None else GtmContact.gmail_thread_id == thread_id
    )).all()
    if len(contacts) != 1:
        if contacts:
            raise MailboxUnavailable("conversation_ambiguous")
        return None
    contact = contacts[0]
    if not enabled():
        return resolve_send_mailbox(session, contact.workspace_id)
    from app.tenancy import current_or_none
    from app.gtm.supymem.pipeline import _gmail_workspace_ids
    scope = current_or_none()
    if scope is not None and scope.workspace_id != "none" and contact.workspace_id not in _gmail_workspace_ids(session, scope.workspace_id):
        raise MailboxUnavailable("conversation_unavailable")
    if thread_id is not None and contact.gmail_thread_id != thread_id:
        raise MailboxUnavailable("conversation_mismatch")
    events = session.scalars(select(GtmEmailEvent).where(
        GtmEmailEvent.contact_id == contact.id, GtmEmailEvent.event_type.in_(["SENT", "sent", "sent_metadata"]),
    ).order_by(GtmEmailEvent.created_at)).all()
    account_ids = set()
    for event in events:
        try:
            metadata = json.loads(event.metadata_json or "{}")
        except (TypeError, ValueError) as exc:
            raise MailboxUnavailable("conversation_unavailable") from exc
        if not isinstance(metadata, dict):
            raise MailboxUnavailable("conversation_unavailable")
        if metadata.get("simulated") is True:
            continue
        if thread_id is None or metadata.get("thread_id") == thread_id:
            if event.email_account_id:
                account_ids.add(event.email_account_id)
    touches = select(GtmTouch.email_account_id).where(
        GtmTouch.contact_id == contact.id, GtmTouch.email_account_id.is_not(None),
        GtmTouch.channel == "email", GtmTouch.simulated == 0,
    )
    if thread_id is not None:
        touches = touches.where(GtmTouch.provider_thread_id == thread_id)
    account_ids.update(session.scalars(touches))
    if not account_ids:
        return None
    if len(account_ids) != 1:
        raise MailboxUnavailable("conversation_ambiguous")
    return _ref(_owned_account(session, contact.workspace_id, account_ids.pop()))


def verify_gmail_connection(workspace_id: str, *, mailbox_id=None) -> dict:
    from app.gtm.supymem.pipeline import verify_gmail_connection as verify

    if not enabled():
        return verify(workspace_id)
    return verify(workspace_id, mailbox_id=mailbox_id)
