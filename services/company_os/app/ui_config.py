from fastapi import HTTPException

from app.credits.policy import get_policy
from app.core.local_mode import is_local


def ui_config(subject: str | None) -> dict:
    if not subject or subject.strip() in {"", "anonymous", "system"}:
        raise HTTPException(401, "Sign in to load your workspace.")
    flags = get_policy("feature_flags")
    local = is_local()
    if local:
        from app.inbox.store import enabled as inbox_enabled
        from app.tasks.routines import enabled as routines_enabled

        routines_on = routines_enabled()
        inbox_on = inbox_enabled()
    else:
        routines_on = isinstance(flags, dict) and flags.get("routines_v1") is True
        inbox_on = isinstance(flags, dict) and flags.get("inbox_briefing_v1") is True
    default = flags.get("founder_ui") if isinstance(flags, dict) else None
    config = {"founder_ui": default if default in ("classic", "home", "chat") else "chat"}
    if routines_on:
        config["routines_v1"] = True
    if isinstance(flags, dict) and flags.get("goals_v1") is True:
        config["goals_v1"] = True  # only present when on, so flag-off responses stay unchanged
    if local or (isinstance(flags, dict) and flags.get("coding_agents_v1") is True):
        from app.coding_agents_consent import consents
        config["coding_agents_v1"] = True
        config["coding_agents"] = {"consent": consents(subject)}
    if inbox_on:
        from sqlalchemy import select
        from app.authz.workspace import subject_person_id
        from app.db.models import Workspace
        from app.gtm.supymem.pipeline import _mailboxes_query
        from app.inbox import store
        from app.state import db
        founder_id = subject_person_id(subject)
        config["inbox_briefing_v1"] = True
        with db()() as session:
            workspace_ids = session.scalars(select(Workspace.id).where(
                Workspace.owner_id == founder_id, Workspace.archived_at.is_(None))).all()
            if workspace_ids:
                _, values = store.settings(session, founder_id, workspace_ids[0])
                consent_ids = set(session.scalars(select(store.InboxBriefingState.mailbox_id).where(
                    store.InboxBriefingState.founder_id == founder_id, store.InboxBriefingState.consent == 1)))
                accounts = {account.id: account for workspace_id in workspace_ids for account in session.scalars(_mailboxes_query(session, workspace_id))}
                config["inbox_briefing"] = {
                    "settings": {key: values[key] for key in ("timezone", "windows", "quiet_hours", "email_copy", "poll_minutes")},
                    "mailboxes": [{"mailbox_id": account.id, "email": account.email or "", "status": account.status, "consent": account.id in consent_ids}
                        for account in accounts.values()]}
    return config
