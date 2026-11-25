"""Email-Supymem backend, ported into Company OS (docs 115–117).

Upstream UI is not used. Hunter, Bright Data research, Composio Gmail,
research jobs, reply/bounce workers, LinkedIn friendship queue, and the
never-stop lead factory all live here.
"""

from app.gtm.supymem.factory import get_or_create_state, set_run
from app.gtm.supymem.pipeline import (
    enrich_status,
    get_contact,
    import_identities,
    ingest_leads,
    list_contacts,
    list_mailboxes,
    mail_readiness,
    regenerate_contact_draft,
    send_test_email,
    start_gmail_connect,
    start_outreach,
    sync_gmail_accounts,
    tick_send_queue,
    update_contact,
)
from app.gtm.supymem.workers import tick_department

__all__ = [
    "enrich_status",
    "get_contact",
    "get_or_create_state",
    "import_identities",
    "ingest_leads",
    "list_contacts",
    "list_mailboxes",
    "mail_readiness",
    "regenerate_contact_draft",
    "send_test_email",
    "set_run",
    "start_gmail_connect",
    "start_outreach",
    "sync_gmail_accounts",
    "tick_department",
    "tick_send_queue",
    "update_contact",
]
