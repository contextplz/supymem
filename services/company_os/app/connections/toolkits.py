"""Opt-in account connections. Connecting does not authorize any tool execution."""
from __future__ import annotations

import os
from typing import Any

from sqlalchemy import and_, or_, select, update

from app.core.local_mode import is_local
from app.db.models import ContentOAuthState, Workspace
from app.state import db


TOOLKITS: dict[str, dict[str, Any]] = {
    "googlesheets": {
        "name": "Google Sheets", "auth_config_env": "COMPOSIO_GOOGLESHEETS_AUTH_CONFIG_ID",
        "why": "Charlie uses your spreadsheets to compare test results and track what works for you.",
        "goals": ["Compare experiment results", "Track signups"],
    },
    "googledocs": {
        "name": "Google Docs", "auth_config_env": "COMPOSIO_GOOGLEDOCS_AUTH_CONFIG_ID",
        "why": "Charlie uses your documents to understand your plans and prepare research summaries for you.",
        "goals": ["Customer research", "Plan your next test"],
    },
    "googledrive": {
        "name": "Google Drive", "auth_config_env": "COMPOSIO_GOOGLEDRIVE_AUTH_CONFIG_ID",
        "why": "Charlie finds the research and materials you already have so you do not repeat work.",
        "goals": ["Review customer research", "Find your test materials"],
    },
    "notion": {
        "name": "Notion", "auth_config_env": "COMPOSIO_NOTION_AUTH_CONFIG_ID",
        "why": "Charlie uses your notes to connect customer feedback with the next steps you are considering.",
        "goals": ["Review customer feedback", "Plan your next test"],
    },
    "cal": {
        "name": "Cal.com", "auth_config_env": "COMPOSIO_CAL_AUTH_CONFIG_ID",
        "why": "Charlie checks your bookings to see whether people follow through on customer interviews.",
        "goals": ["Customer interviews", "Measure booked meetings"],
    },
    "calendly": {
        "name": "Calendly", "auth_config_env": "COMPOSIO_CALENDLY_AUTH_CONFIG_ID",
        "why": "Charlie checks your bookings to compare which invitations lead to real meetings for you.",
        "goals": ["Customer interviews", "Compare invitation results"],
    },
    "typeform": {
        "name": "Typeform", "auth_config_env": "COMPOSIO_TYPEFORM_AUTH_CONFIG_ID",
        "why": "Charlie reads your survey responses to learn what people need and which ideas interest them.",
        "goals": ["Survey tests", "Compare customer groups"],
    },
    "google_analytics": {
        "name": "Google Analytics", "auth_config_env": "COMPOSIO_GOOGLE_ANALYTICS_AUTH_CONFIG_ID",
        "why": "Charlie reads your traffic and signup results to compare your pages and acquisition channels.",
        "goals": ["Page tests", "Channel tests"],
    },
    "google_search_console": {
        "name": "Google Search Console", "auth_config_env": "COMPOSIO_GOOGLESEARCHCONSOLE_AUTH_CONFIG_ID",
        "why": "Charlie reads how people find your site in search to identify useful topics and channels for you.",
        "goals": ["Search demand research", "Channel tests"],
    },
    "stripe": {
        "name": "Stripe", "auth_config_env": "COMPOSIO_STRIPE_AUTH_CONFIG_ID",
        "why": "Charlie reads your revenue results to check whether your tests lead to paying customers. No payments are created or changed.",
        "goals": ["Read revenue results", "Compare pricing results"], "read_only": True,
    },
    "hubspot": {
        "name": "HubSpot", "auth_config_env": "COMPOSIO_HUBSPOT_AUTH_CONFIG_ID",
        "why": "Charlie reads your customer pipeline to compare which tests lead to real sales conversations.",
        "goals": ["Compare customer groups", "Measure sales progress"],
    },
    "slack": {
        "name": "Slack", "auth_config_env": "COMPOSIO_SLACK_AUTH_CONFIG_ID",
        "why": "Charlie reviews feedback you have access to so you can spot recurring customer problems.",
        "goals": ["Customer feedback research", "Find recurring problems"],
    },
    "github": {
        "name": "GitHub", "auth_config_env": "COMPOSIO_GITHUB_AUTH_CONFIG_ID",
        "why": "Charlie reviews your issues and releases to connect product changes with the results you are testing.",
        "goals": ["Review product feedback", "Track test delivery"],
    },
}
for _slug, _entry in TOOLKITS.items():
    _entry["slug"] = _slug

FOUNDER_TOOLKITS = ("stripe", "hubspot", "typeform", "notion", "googledocs", "googledrive", "slack", "github")


def auth_config_id(slug: str) -> str:
    if is_local():
        from app.connections import local_config
        return "" if local_config.missing(slug) else local_config.auth_config_id(slug)
    return os.environ.get(TOOLKITS[slug]["auth_config_env"], "").strip()


def configured_toolkits() -> list[str]:
    return [slug for slug in TOOLKITS if auth_config_id(slug)]


def mark_reconnect(connected_account_id: str) -> None:
    from app.connections.composio_migration import mark_social_reconnect

    mark_social_reconnect(connected_account_id)
    with db()() as session:
        session.execute(update(ContentOAuthState).where(
            ContentOAuthState.connected_account_id == connected_account_id,
            ContentOAuthState.platform.in_(TOOLKITS), ContentOAuthState.auth_method == "composio",
            ContentOAuthState.outcome == "connected",
        ).values(outcome="needs_reconnect"))
        session.commit()


def accounts(workspace_id: str, slug: str) -> list[dict[str, Any]]:
    if is_local() and not auth_config_id(slug):
        return []
    with db()() as session:
        owner = select(Workspace.owner_id).where(Workspace.id == workspace_id).scalar_subquery()
        rows = session.scalars(select(ContentOAuthState).where(
            ContentOAuthState.workspace_id == workspace_id, ContentOAuthState.platform == slug,
            ContentOAuthState.auth_method == "composio", ContentOAuthState.actor_subject.is_not(None),
            or_(ContentOAuthState.provider_user_id == f"content:{workspace_id}",
                and_(ContentOAuthState.platform.in_(FOUNDER_TOOLKITS), ContentOAuthState.provider_user_id == owner)),
            ContentOAuthState.auth_config_id.is_not(None), ContentOAuthState.connected_account_id.is_not(None),
            ContentOAuthState.consumed_at.is_not(None),
            ContentOAuthState.outcome.in_(["connected", "needs_reconnect", "revoked"]),
        ).order_by(ContentOAuthState.created_at.desc())).all()
        receipts = {}
        for row in rows:
            receipts.setdefault(row.connected_account_id,
                {column.name: getattr(row, column.name) for column in ContentOAuthState.__table__.columns})
        return list(receipts.values())


def sync(workspace_id: str, slug: str) -> None:
    from app.content.composio_social import ComposioSocialError
    from app.content.oauth.callbacks import _verified_connection
    from app.content.oauth.attempts import OAuthError
    from app.connections.composio_migration import missing_connection

    for account in accounts(workspace_id, slug):
        if account["outcome"] == "revoked":
            continue
        try:
            _verified_connection(account)
            outcome = "connected"
        except OAuthError:
            outcome = "needs_reconnect"
        except ComposioSocialError as exc:
            if not missing_connection(exc):
                raise
            outcome = "needs_reconnect"
        with db()() as session:
            session.execute(update(ContentOAuthState).where(
                ContentOAuthState.id == account["id"], ContentOAuthState.workspace_id == workspace_id,
                ContentOAuthState.outcome != "revoked",
            ).values(outcome=outcome))
            session.commit()


def revoke_account(session, workspace_id: str, account_id: str, *, expected_connection_id: str | None = None) -> dict[str, Any] | None:
    from app.content.composio_social import delete_connection
    from app.content.oauth.attempts import OAuthError

    owner = select(Workspace.owner_id).where(Workspace.id == workspace_id).scalar_subquery()
    row = session.scalar(select(ContentOAuthState).where(
        ContentOAuthState.id == account_id, ContentOAuthState.workspace_id == workspace_id,
        ContentOAuthState.platform.in_(TOOLKITS), ContentOAuthState.auth_method == "composio",
        or_(ContentOAuthState.provider_user_id == f"content:{workspace_id}",
            and_(ContentOAuthState.platform.in_(FOUNDER_TOOLKITS), ContentOAuthState.provider_user_id == owner)),
        ContentOAuthState.actor_subject.is_not(None), ContentOAuthState.auth_config_id.is_not(None),
        ContentOAuthState.connected_account_id.is_not(None), ContentOAuthState.consumed_at.is_not(None),
        ContentOAuthState.outcome.in_(["connected", "needs_reconnect", "revoked"]),
    ).with_for_update())
    if row is None:
        return None
    if expected_connection_id is not None and expected_connection_id != row.connected_account_id:
        raise OAuthError("connection_changed_review_again", "Your connection changed. Review it again before disconnecting.")
    if row.outcome != "revoked":
        delete_connection(row.connected_account_id, revoke=True)
        session.execute(update(ContentOAuthState).where(
            ContentOAuthState.workspace_id == workspace_id, ContentOAuthState.platform == row.platform,
            ContentOAuthState.connected_account_id == row.connected_account_id,
            ContentOAuthState.auth_method == "composio",
            ContentOAuthState.outcome.in_(["connected", "needs_reconnect"]),
        ).values(outcome="revoked"))
        session.commit()
    return {"id": row.id, "platform": row.platform, "status": "revoked"}
