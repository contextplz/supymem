"""Venture soft-delete / restore / purge (7-day retention) and pause / resume."""

from __future__ import annotations

import json
import logging
from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

from app.audit.writer import audit
from app.core.local_mode import is_local
from app.db.models import PlannedTask, Venture, Workspace
from app.db.session import serialize_writes
from app.state import db
from core.clock import utcnow

# Nested under workspace.settings_json so it never collides with agent_pack.
PAUSE_SETTINGS_KEY = "venture_pause"

RETENTION_DAYS = 7

# Short word the founder types. Compared case-insensitively after strip.
CONFIRM_PHRASE = "DELETE"


class VentureLifecycleError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def confirmation_phrase() -> str:
    return CONFIRM_PHRASE


def confirmation_matches(typed: str) -> bool:
    return (typed or "").strip().casefold() == CONFIRM_PHRASE.casefold()


def _purge_after(archived_at) -> str:
    return (archived_at + timedelta(days=RETENTION_DAYS)).isoformat()


def soft_delete(venture_id: str, *, confirmation: str, actor_id: str = "founder") -> dict[str, Any]:
    """Archive the venture. Requires exact confirmation phrase. Recoverable for 7 days."""
    if not confirmation_matches(confirmation):
        raise VentureLifecycleError(
            "confirmation_mismatch",
            "Type DELETE to confirm, nothing else.",
        )

    factory = db()
    local = is_local()
    with factory() as session:
        if local:
            serialize_writes(session, key=f"venture-delete:{venture_id}")
        now = utcnow()
        ven = session.get(Venture, venture_id, with_for_update=True)
        if ven is None:
            raise VentureLifecycleError("not_found", "Venture not found.")
        if ven.archived_at is not None:
            raise VentureLifecycleError("already_deleted", "This venture is already in the trash.")

        ven.archived_at = now
        ven.updated_at = now
        wid = ven.workspace_id
        if wid:
            ws = session.get(Workspace, wid)
            if ws is not None:
                ws.archived_at = now
                ws.updated_at = now
                if local and _local_owned_venture_workspace(ws):
                    _bind_local_entry_history(session, venture_id, ws.owner_id)
        session.commit()
        payload = {
            "id": ven.id,
            "name": ven.name,
            "workspace_id": wid,
            "archived_at": ven.archived_at.isoformat(),
            "purge_after": _purge_after(ven.archived_at),
            "retention_days": RETENTION_DAYS,
            "recoverable": True,
        }

    from app.authz.workspace import subject_person_id
    from app.email.venture_receipts import on_receipt

    event_id = audit(
        "venture.soft_deleted",
        venture_id=venture_id,
        workspace_id=wid or venture_id,
        actor_id=subject_person_id(actor_id),
        actor_kind="person",
        surface="api",
        target_ref=venture_id,
        payload={
            "schema": "venture_lifecycle_receipt_v1",
            "archived_at": payload["archived_at"],
            "retention_days": RETENTION_DAYS,
            "purge_after": payload["purge_after"],
        },
    )
    on_receipt(event_id)
    return payload


def restore(venture_id: str, *, actor_id: str = "founder") -> dict[str, Any]:
    factory = db()
    with factory() as session:
        if is_local():
            serialize_writes(session, key=f"venture-delete:{venture_id}")
        now = utcnow()
        ven = session.get(Venture, venture_id, with_for_update=True)
        if ven is None:
            raise VentureLifecycleError("not_found", "Venture not found.")
        if ven.archived_at is None:
            raise VentureLifecycleError("not_deleted", "This venture is not in the trash.")
        archived = ven.archived_at
        if archived.tzinfo is None and now.tzinfo is not None:
            from datetime import timezone

            archived = archived.replace(tzinfo=timezone.utc)
        elif archived.tzinfo is not None and now.tzinfo is None:
            archived = archived.replace(tzinfo=None)
        deadline = archived + timedelta(days=RETENTION_DAYS)
        if now > deadline or ven.stage == "purged":
            raise VentureLifecycleError(
                "retention_expired",
                "The 7-day recovery window has ended. This venture can no longer be restored.",
            )
        ven.archived_at = None
        ven.updated_at = now
        if ven.workspace_id:
            ws = session.get(Workspace, ven.workspace_id)
            if ws is not None:
                ws.archived_at = None
                ws.updated_at = now
        session.commit()
        wid = ven.workspace_id
        out = {"id": ven.id, "name": ven.name, "restored": True, "workspace_id": wid}

    if wid:
        audit(
            "venture.restored",
            workspace_id=wid,
            actor_id=actor_id,
            actor_kind="person",
            surface="api",
            target_ref=venture_id,
            payload={},
        )
    return out


def _heal_names(session, rows: list[Venture]) -> bool:
    """Replace clipped sentence titles with a real product name and persist."""
    from app.db.supymem_models import EntryChatProposal
    from app.intake.naming import derive_name, needs_heal

    changed = False
    now = utcnow()
    for row in rows:
        if not needs_heal(row.name):
            continue
        chosen = False
        for proposal in session.scalars(select(EntryChatProposal).where(
                EntryChatProposal.target_venture_id == row.id,
                EntryChatProposal.state.in_(("committing", "committed")))):
            try:
                saved = json.loads(proposal.receipt_json or "{}")
            except (TypeError, ValueError):
                continue
            if not isinstance(saved, dict):
                continue
            if proposal.state == "committed":
                chosen = (isinstance(saved.get("name"), str) and bool(saved["name"])
                          and saved["name"] == row.name and saved.get("venture_id") == row.id
                          and saved.get("workspace_id") == row.workspace_id)
            else:
                # The creation transaction binds this target before returning it. The validated
                # choice also protects a concurrent list before its confirmation receipt is saved.
                name = saved.get("chosen_name")
                workspace = session.get(Workspace, row.workspace_id) if row.workspace_id else None
                suffix = (row.name or "").rsplit(" ", 1)[-1]
                chosen = (isinstance(name, str) and bool(name) and workspace is not None
                          and workspace.owner_id == proposal.actor_id and saved.get("created_new", True) is True
                          and (row.name == name or (len(row.name or "") <= 48 and suffix.isascii()
                               and suffix.isdecimal() and not suffix.startswith("0") and int(suffix) >= 2
                               and row.name == name[:48 - len(suffix) - 1] + " " + suffix)))
            if chosen:
                break
        if chosen:
            continue
        next_name = derive_name(None, row.raw_idea or "")
        if not next_name or next_name == (row.name or "").strip():
            continue
        row.name = next_name
        row.updated_at = now
        changed = True
    return changed


def list_active(*, workspace_ids: set[str] | None = None, summary: bool = False) -> list[dict[str, Any]]:
    factory = db()
    with factory() as session:
        stmt = select(Venture).where(Venture.archived_at.is_(None))
        if workspace_ids is not None:
            if not workspace_ids:
                return []
            stmt = stmt.where(Venture.workspace_id.in_(workspace_ids))
        rows = session.scalars(stmt.order_by(Venture.created_at.desc())).all()
        if summary:
            return _list_summaries(session, rows)
        settings = _workspace_settings(session, rows)
        if _heal_names(session, rows):
            session.commit()
        active = []
        for row in rows:
            try:
                active.append(_ser_active(row, settings.get(row.workspace_id, {})))
            except Exception as exc:
                logging.getLogger(__name__).warning("active venture projection skipped", extra={
                    "venture_id": row.id, "error_class": type(exc).__name__,
                })
        return active


def list_deleted(*, workspace_ids: set[str] | None = None, summary: bool = False) -> list[dict[str, Any]]:
    factory = db()
    now = utcnow()
    with factory() as session:
        stmt = select(Venture).where(Venture.archived_at.is_not(None))
        if workspace_ids is not None:
            if not workspace_ids:
                return []
            stmt = stmt.where(Venture.workspace_id.in_(workspace_ids))
        rows = session.scalars(stmt.order_by(Venture.archived_at.desc())).all()
        summaries = {item["id"]: item for item in _list_summaries(session, rows)} if summary else {}
        settings = _workspace_settings(session, rows) if not summary else {}
        items = []
        for r in rows:
            assert r.archived_at is not None
            archived = r.archived_at
            if archived.tzinfo is None and now.tzinfo is not None:
                from datetime import timezone

                archived = archived.replace(tzinfo=timezone.utc)
            elif archived.tzinfo is not None and now.tzinfo is None:
                archived = archived.replace(tzinfo=None)
            deadline = archived + timedelta(days=RETENTION_DAYS)
            days_left = max(0, (deadline - now).days)
            if now <= deadline and r.stage != "purged":
                items.append(
                    {
                        **(summaries[r.id] if summary else _ser_active(r, settings.get(r.workspace_id, {}))),
                        "archived_at": r.archived_at.isoformat(),
                        "purge_after": deadline.isoformat(),
                        "days_left": days_left,
                        "recoverable": True,
                    }
                )
        return items


def purge_expired() -> list[str]:
    """Redact expired Trash entries and clear scoped local setup and chat, not idea history."""
    factory = db()
    now = utcnow()
    purged: list[str] = []
    local = is_local()
    with factory() as session:
        if local:
            serialize_writes(session, key="local-venture-retention")
        rows = session.scalars(
            select(Venture).where(Venture.archived_at.is_not(None), Venture.stage != "purged").with_for_update()
        ).all()
        for ven in rows:
            archived = ven.archived_at
            if archived is None:
                continue
            if archived.tzinfo is None and now.tzinfo is not None:
                from datetime import timezone

                archived = archived.replace(tzinfo=timezone.utc)
            elif archived.tzinfo is not None and now.tzinfo is None:
                archived = archived.replace(tzinfo=None)
            if archived >= now - timedelta(days=RETENTION_DAYS):
                continue
            vid = ven.id
            wid = ven.workspace_id
            ws = session.get(Workspace, wid) if wid else None
            if local:
                if not _local_owned_venture_workspace(ws) or ws.archived_at != ven.archived_at:
                    continue
                _purge_local_onboarding(session, vid, ws)
            ven.name = f"Deleted {vid[-6:]}"
            ven.raw_idea = "[purged after retention]"
            ven.stage = "purged"
            ven.updated_at = now
            if ws is not None:
                ws.name = f"Deleted {vid[-6:]}"
                ws.updated_at = now
            purged.append(vid)
        if purged:
            session.commit()
    return purged


def _local_owned_venture_workspace(workspace: Workspace | None) -> bool:
    from app.workspaces.resolve import LOCAL_PERSON_ID

    return workspace is not None and workspace.kind == "venture" and workspace.owner_id == LOCAL_PERSON_ID


def _bind_local_entry_history(session, venture_id: str, actor_id: str) -> None:
    from app.db.supymem_models import EntryChatProposal

    rows = session.scalars(select(EntryChatProposal).where(EntryChatProposal.target_venture_id == venture_id,
                                                          EntryChatProposal.actor_id == actor_id)).all()
    seen = {row.id for row in rows}
    # shortcut: old turns without a saved link are left alone, recover them only with verifiable ownership.
    for row in rows:
        while True:
            try:
                metadata = json.loads(row.receipt_json or "{}")
            except ValueError:
                break
            parent_id = metadata.get("_parent_proposal_id") if isinstance(metadata, dict) else None
            if not isinstance(parent_id, str) or parent_id in seen:
                break
            parent = session.get(EntryChatProposal, parent_id)
            if parent is None or parent.actor_id != actor_id or parent.target_venture_id is not None:
                break
            parent.target_venture_id = venture_id
            seen.add(parent_id)
            row = parent


def _purge_local_onboarding(session, venture_id: str, workspace: Workspace) -> None:
    from app.db.models import ChatContextCheckpoint, ChatConversation, ChatMessage, VentureDocument
    from app.db.supymem_models import ClarifierTurn, EntryChatProposal, IdeaClarificationRun, IdeaSession

    conversation_ids = select(ChatConversation.id).where(ChatConversation.venture_id == venture_id)
    session.execute(delete(ChatContextCheckpoint).where(ChatContextCheckpoint.conversation_id.in_(conversation_ids)))
    for model in (ChatMessage, ChatConversation, IdeaClarificationRun, ClarifierTurn):
        session.execute(delete(model).where(model.venture_id == venture_id))
    # Revision history is append-only, even in SQLite.
    session.query(IdeaSession).filter_by(venture_id=venture_id).update({"recon_summary": "{}"})
    _bind_local_entry_history(session, venture_id, workspace.owner_id)
    session.flush()
    session.execute(delete(EntryChatProposal).where(EntryChatProposal.target_venture_id == venture_id,
                                                    EntryChatProposal.actor_id == workspace.owner_id))
    session.execute(delete(VentureDocument).where(VentureDocument.venture_id == venture_id,
        VentureDocument.kind.in_(("founder_evidence", "external_site", "founder_assets", "intake_source", "intake_decision"))))
    try:
        settings = json.loads(workspace.settings_json or "{}")
    except ValueError:
        return
    if isinstance(settings, dict):
        for field in ("intake", "intake_path", "intake_venture_id"):
            settings.pop(field, None)
        workspace.settings_json = json.dumps(settings)


def is_active(venture_id: str) -> Venture | None:
    factory = db()
    with factory() as session:
        ven = session.get(Venture, venture_id)
        if ven is None or ven.archived_at is not None or ven.stage == "purged":
            return None
        session.expunge(ven)
        return ven


def _pause_file(venture_id: str, *, create: bool = False) -> Path:
    from app.config import data_root

    folder = data_root() / "ventures" / venture_id
    if create:
        folder.mkdir(parents=True, exist_ok=True)
    return folder / "pause.json"


def _read_json_file(path: Path) -> dict[str, Any]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return raw if isinstance(raw, dict) else {}


def _settings_dict(raw: str | None) -> dict[str, Any]:
    try:
        parsed = json.loads(raw or "{}")
    except json.JSONDecodeError:
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


def _load_pause_blob(venture_id: str, *, workspace_id: str | None = None) -> dict[str, Any]:
    """Durable pause record: workspace.settings_json, then pause.json on disk."""
    if workspace_id:
        factory = db()
        with factory() as session:
            ws = session.get(Workspace, workspace_id)
            if ws is not None:
                blob = _settings_dict(ws.settings_json).get(PAUSE_SETTINGS_KEY)
                if isinstance(blob, dict) and blob.get("paused"):
                    return blob
    file_blob = _read_json_file(_pause_file(venture_id))
    if file_blob.get("paused"):
        return file_blob
    return {}


def _write_pause_blob(
    session,
    ven: Venture,
    blob: dict[str, Any] | None,
) -> None:
    """Persist or clear pause state on the workspace row and the on-disk file."""
    now = utcnow()
    ven.updated_at = now
    if ven.workspace_id:
        ws = session.get(Workspace, ven.workspace_id)
        if ws is not None:
            settings = _settings_dict(ws.settings_json)
            if blob:
                settings[PAUSE_SETTINGS_KEY] = blob
            else:
                settings.pop(PAUSE_SETTINGS_KEY, None)
            ws.settings_json = json.dumps(settings)
            ws.updated_at = now
    path = _pause_file(ven.id, create=True)
    if blob:
        path.write_text(json.dumps(blob, indent=2), encoding="utf-8")
    elif path.exists():
        try:
            path.unlink()
        except OSError:
            path.write_text("{}", encoding="utf-8")


def pause_fields(venture_id: str, *, workspace_id: str | None = None) -> dict[str, Any]:
    return _pause_fields(_load_pause_blob(venture_id, workspace_id=workspace_id))


def _pause_fields(blob: dict[str, Any]) -> dict[str, Any]:
    if not blob.get("paused"):
        return {"paused": False, "paused_reason": None, "paused_at": None}
    reason = blob.get("paused_reason")
    if isinstance(reason, str):
        reason = reason.strip() or None
    else:
        reason = None
    at = blob.get("paused_at")
    return {
        "paused": True,
        "paused_reason": reason,
        "paused_at": at if isinstance(at, str) else None,
    }


def is_paused(venture_id: str) -> bool:
    factory = db()
    with factory() as session:
        ven = session.get(Venture, venture_id)
        if ven is None or ven.archived_at is not None or ven.stage == "purged":
            return False
        workspace_id = ven.workspace_id
    return bool(_load_pause_blob(venture_id, workspace_id=workspace_id).get("paused"))


def is_workspace_paused(workspace_id: str) -> bool:
    """Scheduler/autopilot guard: skip work for a paused venture workspace."""
    if not workspace_id:
        return False
    factory = db()
    with factory() as session:
        ws = session.get(Workspace, workspace_id)
        if ws is None:
            return False
        blob = _settings_dict(ws.settings_json).get(PAUSE_SETTINGS_KEY)
        if isinstance(blob, dict) and blob.get("paused"):
            return True
        ven = session.scalars(
            select(Venture).where(Venture.workspace_id == workspace_id)
        ).first()
        if ven is None:
            return False
        vid = ven.id
    file_blob = _read_json_file(_pause_file(vid))
    return bool(file_blob.get("paused"))


def _require_active(session, venture_id: str) -> Venture:
    ven = session.get(Venture, venture_id)
    if ven is None or ven.archived_at is not None or ven.stage == "purged":
        raise VentureLifecycleError("not_found", "Venture not found.")
    return ven


def pause(venture_id: str, *, reason: str | None = None, actor_id: str = "founder") -> dict[str, Any]:
    """Stop spend and work. Reversible; roster stays hired."""
    typed = (reason or "").strip() or None
    actor = (actor_id or "").strip() or "founder"
    factory = db()
    now = utcnow()
    with factory() as session:
        ven = _require_active(session, venture_id)
        existing = _load_pause_blob(ven.id, workspace_id=ven.workspace_id)
        if existing.get("paused"):
            raise VentureLifecycleError("already_paused", "This venture is already paused.")

        from app.onboarding.orchestrator import _pause as pause_onboarding
        from app.onboarding.state import active_run

        run = active_run(ven.id)
        stopped_onboarding = False
        paused_run_id: str | None = None
        if run is not None and run.status == "running":
            pause_onboarding(run, "venture_paused")
            stopped_onboarding = True
            paused_run_id = run.id

        running_tasks = list(
            session.scalars(
                select(PlannedTask).where(
                    PlannedTask.venture_id == ven.id,
                    PlannedTask.status == "running",
                )
            ).all()
        )
        for task in running_tasks:
            task.status = "open"
            task.updated_at = now
        halted_tasks = len(running_tasks)

        blob = {
            "paused": True,
            "paused_at": now.isoformat(),
            "paused_reason": typed,
            "paused_onboarding_run_id": paused_run_id,
        }
        _write_pause_blob(session, ven, blob)
        session.commit()
        session.refresh(ven)
        session.expunge(ven)
        wid = ven.workspace_id
        name = ven.name

    extra_ids: list[str] = []
    try:
        from app.org.summary import resolve_org

        for agent in resolve_org(venture_id).get("agents") or []:
            aid = str(agent.get("id") or "").strip()
            if aid:
                extra_ids.append(aid)
    except Exception:
        extra_ids = []

    from app.org.heartbeat import mark_all_idle

    halted_agents = mark_all_idle(venture_id, extra_agent_ids=extra_ids)

    audit(
        "venture.paused",
        workspace_id=wid or venture_id,
        actor_id=actor,
        actor_kind="person",
        surface="api",
        target_ref=venture_id,
        payload={
            "reason": typed,
            "stopped_onboarding": stopped_onboarding,
            "stopped_agents": halted_agents,
            "stopped_tasks": halted_tasks,
        },
    )
    ser = _ser_active(ven)
    return {
        "id": ven.id,
        "name": name,
        "paused": True,
        "paused_at": blob["paused_at"],
        "paused_reason": typed,
        "status": ser.get("status"),
        "stopped": {
            "onboarding": stopped_onboarding,
            "agents": halted_agents,
            "tasks": halted_tasks,
        },
    }


async def resume(venture_id: str, *, actor_id: str = "founder") -> dict[str, Any]:
    """Clear pause and optionally restart an onboarding run we halted."""
    actor = (actor_id or "").strip() or "founder"
    factory = db()
    now = utcnow()
    with factory() as session:
        ven = _require_active(session, venture_id)
        existing = _load_pause_blob(ven.id, workspace_id=ven.workspace_id)
        if not existing.get("paused"):
            raise VentureLifecycleError("not_paused", "This venture is not paused.")
        restart_run_id = existing.get("paused_onboarding_run_id")
        if not isinstance(restart_run_id, str) or not restart_run_id.strip():
            restart_run_id = None
        else:
            restart_run_id = restart_run_id.strip()
        _write_pause_blob(session, ven, None)
        session.commit()
        session.refresh(ven)
        session.expunge(ven)
        wid = ven.workspace_id
        name = ven.name

    restarted = False
    if restart_run_id:
        from app.onboarding.spawn import start_onboarding
        from app.onboarding.state import load_run

        run = load_run(restart_run_id)
        # Only restart a run we paused. Never replay a finished run.
        if run is not None and run.status == "paused" and run.paused_reason == "venture_paused":
            await start_onboarding(
                venture_id,
                mode=run.mode or "guided",
                resume=True,
                run_id=run.id,
                entry_mode=getattr(run, "entry_mode", None) or "idea",
                workspace_id=getattr(run, "workspace_id", None) or wid,
            )
            restarted = True
        elif run is not None and run.status == "running":
            restarted = True

    audit(
        "venture.resumed",
        workspace_id=wid or venture_id,
        actor_id=actor,
        actor_kind="person",
        surface="api",
        target_ref=venture_id,
        payload={"resumed_onboarding": restarted},
    )
    ser = _ser_active(ven)
    return {
        "id": ven.id,
        "name": name,
        "paused": False,
        "resumed_at": now.isoformat(),
        "status": ser.get("status"),
        "resumed": {"onboarding": restarted},
    }


def _venture_fields(r: Venture) -> dict[str, Any]:
    return {
        "id": r.id,
        "name": r.name,
        "stage": r.stage,
        "raw_idea": r.raw_idea,
        "workspace_id": r.workspace_id,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


def _workspace_settings(session, rows: list[Venture]) -> dict[str, dict[str, Any]]:
    workspace_ids = {row.workspace_id for row in rows if row.workspace_id}
    return {workspace_id: _settings_dict(raw) for workspace_id, raw in session.execute(select(Workspace.id, Workspace.settings_json)
        .where(Workspace.id.in_(workspace_ids))).all()} if workspace_ids else {}


def _list_summaries(session, rows: list[Venture]) -> list[dict[str, Any]]:
    settings = _workspace_settings(session, rows)
    items = []
    for row in rows:
        workspace_settings = settings.get(row.workspace_id, {})
        blob = workspace_settings.get(PAUSE_SETTINGS_KEY)
        if not isinstance(blob, dict) or not blob.get("paused"):
            blob = _read_json_file(_pause_file(row.id))
        # ponytail: live status and team counts are deferred to detail endpoints; batch receipts if the hub needs them.
        from app.core.local_mode import sample_view
        items.append(sample_view({**_venture_fields(row), **_pause_fields(blob), **card_fields(workspace_settings)},
            workspace_settings.get("sample") is True))
    return items


_CARD_TYPES = frozenset({
    "software-ai",
    "marketplace",
    "consumer-brand",
    "service-business",
    "media-community",
    "hardware",
})
_CARD_COLORS = frozenset({"blue", "green", "pink", "orange"})
_CARD_ART = frozenset({"3", "4", "5", "6", "7", "8", "9", "10"})


def card_fields(settings: dict[str, Any]) -> dict[str, str | None]:
    """Founder-chosen tile art, stored on the workspace so it follows the venture."""
    card_type = settings.get("card_type")
    card_color = settings.get("card_color")
    card_art = settings.get("card_art")
    return {
        "card_type": card_type if isinstance(card_type, str) and card_type in _CARD_TYPES else None,
        "card_color": card_color if isinstance(card_color, str) and card_color in _CARD_COLORS else None,
        "card_art": card_art if isinstance(card_art, str) and card_art in _CARD_ART else None,
    }


def _ser_active(r: Venture, settings: dict[str, Any] | None = None) -> dict[str, Any]:
    from app.memory.scope import bound_memory_scope
    if settings is None:
        from app.workspaces.service import get_workspace
        workspace = get_workspace(r.workspace_id) if r.workspace_id else None
        settings = workspace.settings if workspace else {}
    with bound_memory_scope(r.id, workspace_id=r.workspace_id):
        from app.core.local_mode import sample_view
        return sample_view({**_ser_active_scoped(r), **card_fields(settings)}, settings.get("sample") is True)


def _ser_active_scoped(r: Venture) -> dict[str, Any]:
    from app.onboarding.state import latest_run
    from app.org.summary import operational_status, published_site_is_public, resolve_org, summarize_agents
    from app.state import MEM, db as db_factory
    from app.db.models import SiteVersion
    from app.db.supymem_models import Deployment
    from sqlalchemy import select

    run = latest_run(r.id)
    agents = (resolve_org(r.id).get("agents") or []) if r.id else []
    summary = summarize_agents(agents, venture_id=r.id)
    has_site = False
    website_published = False
    website_mem = MEM.get(f"website:{r.id}") or {}
    if website_mem.get("url") or website_mem.get("published"):
        has_site = True
        website_published = published_site_is_public(
            bool(website_mem.get("published")), website_mem.get("url"))
    if r.workspace_id:
        factory = db_factory()
        with factory() as session:
            row = session.scalars(
                select(SiteVersion)
                .where(SiteVersion.workspace_id == r.workspace_id)
                .order_by(SiteVersion.version.desc())
                .limit(1)
            ).first()
            has_site = has_site or bool(row)
            website_published = website_published or bool(row and published_site_is_public(
                bool(row.published), row.url))
            live_deployment = session.scalar(
                select(Deployment.id)
                .where(
                    Deployment.venture_id == r.id,
                    Deployment.state == "live",
                )
                .limit(1)
            )
            has_site = has_site or bool(live_deployment)
            website_published = website_published or bool(live_deployment)
    pause = pause_fields(r.id, workspace_id=r.workspace_id)
    if pause["paused"]:
        onboarding_status = "paused"
        status_reason = pause["paused_reason"]
    else:
        onboarding_status = run.status if run else None
        status_reason = getattr(run, "paused_reason", None) if run else None
    status = operational_status(
        stage=r.stage,
        onboarding_status=onboarding_status,
        paused_reason=status_reason,
        agent_summary=summary,
        signals={
            "has_site": has_site,
            "has_team": bool(summary["hired"]),
            "website_published": website_published,
        },
        founder_paused=pause["paused"],
    )
    return {
        **_venture_fields(r),
        "status": status,
        "paused": pause["paused"],
        "paused_reason": pause["paused_reason"],
        "paused_at": pause["paused_at"],
        "agents": {
            "hired": summary["hired"],
            "working": summary["working"],
            "watching": summary["watching"],
        },
    }


__all__ = [
    "CONFIRM_PHRASE",
    "PAUSE_SETTINGS_KEY",
    "RETENTION_DAYS",
    "VentureLifecycleError",
    "confirmation_matches",
    "confirmation_phrase",
    "is_active",
    "is_paused",
    "is_workspace_paused",
    "list_active",
    "list_deleted",
    "pause",
    "pause_fields",
    "purge_expired",
    "restore",
    "resume",
    "soft_delete",
]
