"""Per-owner consent for Charlie to use your own coding agents.

Stores only provider names and timestamps, never credentials. Consent does not
authorize any task: the owner must request the work separately.
The legacy packages/coding_agents/consent.py store is a local, global SQLite file
for one Mac, so it cannot hold hosted per-founder consent.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from fastapi import HTTPException
from sqlalchemy import DateTime, Text, UniqueConstraint, delete, select
from sqlalchemy.orm import Mapped, mapped_column

from app.credits.policy import get_policy
from app.core.local_mode import is_local
from app.db.models import Base

PROVIDERS = ("claude", "codex", "cursor", "opencode")


class CodingAgentConsent(Base):
    __tablename__ = "coding_agent_consent"
    id: Mapped[str] = mapped_column(Text, primary_key=True)
    founder_id: Mapped[str] = mapped_column(Text, nullable=False, index=True)
    provider: Mapped[str] = mapped_column(Text, nullable=False)
    consented_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    __table_args__ = (UniqueConstraint("founder_id", "provider", name="uq_coding_agent_consent_founder_provider"),)


TABLES = (CodingAgentConsent.__table__,)


def ensure_schema(engine):
    Base.metadata.create_all(engine, tables=TABLES)


def enabled() -> bool:
    """Connection choices stay editable even when build execution is switched off."""
    if is_local():
        return True
    flags = get_policy("feature_flags")
    return isinstance(flags, dict) and flags.get("coding_agents_v1") is True


def _founder(subject: str | None) -> str:
    if not subject or subject.strip() in {"", "anonymous", "system"}:
        raise HTTPException(401, "Reopen your local app, then open Settings > Keys and connections > Coding tools." if is_local()
                            else "Sign in to load your workspace.")
    from app.authz.workspace import subject_person_id
    return subject_person_id(subject)


def consents(subject: str | None) -> dict[str, bool]:
    from app.state import db
    founder_id = _founder(subject)
    with db()() as session:
        allowed = set(session.scalars(select(CodingAgentConsent.provider).where(CodingAgentConsent.founder_id == founder_id)))
    return {provider: provider in allowed for provider in PROVIDERS}


def _check_provider(provider: str) -> None:
    if provider not in PROVIDERS:
        raise HTTPException(400, detail={"code": "unsupported_provider",
                            "message": "Open Settings > Keys and connections > Coding tools and choose your installed coding tool."})


def _pin_connected_provider(provider: str, candidate: str | None) -> None:
    from desktop_runtime.agent_status import pin_provider
    from desktop_runtime.owned_coding import coding_connection_message

    if candidate == "":
        raise HTTPException(409, "Open Settings > Keys and connections > Coding tools, select Re-detect coding tools, then review the tool location and turn on Allow.")
    try:
        pin_provider(provider, candidate=candidate)
    except (OSError, ValueError, RuntimeError) as error:
        if str(error) == "coding_git_not_installed":
            raise HTTPException(409, detail={"code": str(error), "message": coding_connection_message(str(error), provider)}) from None
        if str(error) == "coding_provider_unsafe_location":
            raise HTTPException(409, coding_connection_message(str(error), provider) or "Your coding tool was found in an unsafe location. Reinstall it in a folder only you or your administrator can change, then select Re-detect coding tools in Settings > Keys and connections > Coding tools.") from None
        raise HTTPException(409, "Sign in with your coding tool subscription, then open Settings > Keys and connections > Coding tools, select Re-detect coding tools and approve the displayed location again.") from None


def check_connection(subject: str | None, provider: str, candidate: str | None = None) -> None:
    if not is_local():
        raise HTTPException(404, "Not found")
    _check_provider(provider)
    if consents(subject)[provider]:
        from desktop_runtime.agent_status import _status, provider_pin
        from desktop_runtime.owned_coding import coding_connection_message

        try:
            pin = provider_pin(provider)
        except (OSError, ValueError, RuntimeError):
            pin = None
        if pin is None:
            code = "coding_tool_not_connected"
            raise HTTPException(409, detail={"code": code, "message": coding_connection_message(code, provider)})
        status = _status(provider)
        if status.get("build_blocked_message"):
            raise HTTPException(409, detail={"code": "coding_git_not_installed", "message": status["build_blocked_message"]})
        if candidate or not status["available"]:
            _pin_connected_provider(provider, candidate or "")


def raise_if_tool_changed(allowed: dict, tools: list[dict], provider: str | None = None) -> None:
    from desktop_runtime.owned_coding import _PROVIDER_BINARIES, coding_connection_message

    for tool in tools:
        name = tool.get("name")
        if (allowed.get(name) is True and tool.get("build_blocked_message")
                and (provider is None or provider == name)):
            raise HTTPException(409, detail={"code": "coding_git_not_installed", "message": tool["build_blocked_message"]})
        if (name in _PROVIDER_BINARIES and allowed.get(name) is True
                and (tool.get("changed") or tool.get("candidate") or tool.get("unsafe_location"))
                and (provider is None or provider == name)):
            if tool.get("unsafe_location_message"):
                code = "coding_provider_unsafe_location"
            elif tool.get("candidate") and not tool.get("changed"):
                code = "coding_tool_not_connected"
            else:
                code = "coding_provider_changed"
            raise HTTPException(409, detail={"code": code, "message": coding_connection_message(code, name)})


def set_consent(subject: str | None, provider: str, allow: bool, candidate: str | None = None) -> dict[str, bool]:
    if not enabled():
        raise HTTPException(404, "Not found")
    founder_id = _founder(subject)
    _check_provider(provider)
    local = is_local()
    if local and allow:
        _pin_connected_provider(provider, candidate)
    from app.state import db
    with db()() as session:
        session.execute(delete(CodingAgentConsent).where(
            CodingAgentConsent.founder_id == founder_id, CodingAgentConsent.provider == provider))
        if allow:
            session.add(CodingAgentConsent(id=uuid.uuid4().hex, founder_id=founder_id, provider=provider,
                consented_at=datetime.now(timezone.utc).replace(tzinfo=None)))
        session.commit()
    if local and not allow:
        from desktop_runtime.agent_status import pin_provider

        try:
            pin_provider(provider, allow=False)
        except (OSError, ValueError, RuntimeError):
            raise HTTPException(409, "Allow is off, but your connection record could not be reset. Open Settings > Keys and connections > Your data folder and backup, restore a trusted backup, then check your coding tool again.") from None
    return consents(subject)
