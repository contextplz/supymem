"""Persist and query SiteVersion rows (45 §5)."""

from __future__ import annotations

import base64
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from app.db.models import SiteVersion, Venture
from app.state import db
from core.clock import utcnow
from core.ids import new_id

MAX_VARIANTS = 5


@dataclass(frozen=True)
class SiteVersionView:
    id: str
    workspace_id: str
    version: int
    stage: str
    template: str
    archetype: str
    brandkit_hash: str
    copy_hash: str
    variant_index: int
    path: str
    url: str | None
    screenshot_path: str | None
    sha256: str
    fallback: bool
    published: bool
    cost_usd: float
    created_at: str
    copy: dict[str, Any]
    brandkit: dict[str, Any]
    venture_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "venture_id": self.venture_id,
            "version": self.version,
            "stage": self.stage,
            "template": self.template,
            "archetype": self.archetype,
            "brandkit_hash": self.brandkit_hash,
            "copy_hash": self.copy_hash,
            "variant_index": self.variant_index,
            "path": self.path,
            "url": self.url,
            "screenshot_path": self.screenshot_path,
            "sha256": self.sha256,
            "fallback": self.fallback,
            "published": self.published,
            "cost_usd": self.cost_usd,
            "created_at": self.created_at,
            "copy": self.copy,
            "brandkit": self.brandkit,
        }


def _parse_json(raw: str | None) -> dict[str, Any]:
    try:
        data = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return data if isinstance(data, dict) else {}


def view(row: SiteVersion) -> SiteVersionView:
    return SiteVersionView(
        id=row.id,
        workspace_id=row.workspace_id,
        venture_id=row.venture_id,
        version=row.version,
        stage=row.stage,
        template=row.template,
        archetype=row.archetype,
        brandkit_hash=row.brandkit_hash,
        copy_hash=row.copy_hash,
        variant_index=row.variant_index,
        path=row.path,
        url=row.url,
        screenshot_path=row.screenshot_path,
        sha256=row.sha256,
        fallback=bool(row.fallback),
        published=bool(row.published),
        cost_usd=float(row.cost_usd or 0.0),
        created_at=row.created_at.isoformat() if row.created_at else "",
        copy=_parse_json(row.copy_json),
        brandkit=_parse_json(row.brandkit_json),
    )


def next_version(workspace_id: str) -> int:
    factory = db()
    with factory() as session:
        current = session.scalar(
            select(func.max(SiteVersion.version)).where(
                SiteVersion.workspace_id == workspace_id
            )
        )
    return int(current or 0) + 1


def validate_binding(workspace_id: str, venture_id: str | None) -> None:
    """Do not attribute another venture's writer to the current founder scope."""
    from app.tenancy import current_or_none
    scope = current_or_none()
    if scope and (scope.workspace_id != workspace_id or
                  (scope.venture_id and scope.venture_id != venture_id)):
        raise PermissionError("site_scope_mismatch")
    if venture_id is None:
        return  # legacy workspace-only API remains explicitly unbound
    with db()() as session:
        venture = session.get(Venture, venture_id)
        if not venture or venture.workspace_id != workspace_id or venture.archived_at is not None:
            raise PermissionError("site_scope_mismatch")
    if scope:
        from app.authz.workspace import assert_venture_access, subject_person_id
        from app.workspaces.service import role_of
        if assert_venture_access(venture_id, scope.actor)[1] != workspace_id or role_of(
                workspace_id, subject_person_id(scope.actor)) not in {"owner", "admin", "maintainer", "contributor"}:
            raise PermissionError("site_write_forbidden")


def persist(
    *,
    workspace_id: str,
    stage: str,
    template: str,
    archetype: str,
    brandkit_hash: str,
    copy_hash: str,
    copy: dict[str, Any],
    brandkit: dict[str, Any],
    variant_index: int,
    path: str,
    url: str | None,
    sha256: str,
    fallback: bool = False,
    cost_usd: float = 0.0,
    screenshot_path: str | None = None,
    published: bool = False,
    venture_id: str | None = None,
) -> SiteVersionView:
    validate_binding(workspace_id, venture_id)
    version = next_version(workspace_id)
    copy = dict(copy)
    # Freeze rendered bytes and the current logo with this version. Public reads
    # must not pick up a later draft from a mutable file or uploaded brand asset.
    rendered = Path(path)
    snapshot = {"files": dict(copy.get("page_files") or {})}
    if rendered.is_file():
        raw = rendered.read_bytes()
        if hashlib.sha256(raw).hexdigest() == sha256:
            snapshot["html"] = raw.decode("utf-8")
    from app.firstlook.brand_assets import custom_logo_path
    logo = custom_logo_path(workspace_id, venture_id)
    if logo is not None:
        snapshot["logo"] = {"suffix": logo.suffix.lower(), "data": base64.b64encode(logo.read_bytes()).decode("ascii")}
    elif venture_id:
        previous = get_latest(workspace_id, venture_id=venture_id)
        previous_logo = previous.copy.get("_render_snapshot", {}).get("logo") if previous else None
        if previous_logo:
            snapshot["logo"] = previous_logo
    copy["_render_snapshot"] = snapshot
    row = SiteVersion(
        id=new_id("site"),
        workspace_id=workspace_id,
        venture_id=venture_id,
        version=version,
        stage=stage,
        template=template,
        archetype=archetype,
        brandkit_hash=brandkit_hash,
        copy_hash=copy_hash,
        copy_json=json.dumps(copy, sort_keys=True),
        brandkit_json=json.dumps(brandkit, sort_keys=True),
        variant_index=variant_index,
        path=path,
        url=url,
        screenshot_path=screenshot_path,
        sha256=sha256,
        fallback=1 if fallback else 0,
        published=1 if published else 0,
        cost_usd=float(cost_usd or 0.0),
        created_at=utcnow(),
    )
    factory = db()
    with factory() as session:
        session.add(row)
        session.commit()
        session.refresh(row)
        session.expunge(row)
    from app.analytics.funnel import emit_for_venture
    from app.analytics.error_events import actor_person_id
    from app.tenancy import current_or_none
    scope = current_or_none()
    emit_for_venture("site.draft_generated", venture_id, key=f"site:{row.id}", workspace_id=workspace_id,
                     person_id=actor_person_id(scope.actor if scope else None),
                     props={"stage": stage, "fallback": bool(fallback)})
    return view(row)


def list_versions(workspace_id: str) -> list[SiteVersionView]:
    factory = db()
    with factory() as session:
        rows = session.scalars(
            select(SiteVersion)
            .where(SiteVersion.workspace_id == workspace_id)
            .order_by(SiteVersion.version.desc())
        ).all()
        for row in rows:
            session.expunge(row)
    return [view(r) for r in rows]


def record_generated_draft(identifier: str) -> None:
    """Certify saved draft bytes only, never publication or requested business outcomes."""
    from app.tenancy import current_or_none
    scope = current_or_none()
    if not scope or not scope.venture_id:
        return
    from app.autopilot.receipts import persisted_record
    from app.firstlook.copy import copy_hash
    with db()() as session:
        row = session.get(SiteVersion, identifier)
        if (not row or (row.workspace_id, row.venture_id) != (scope.workspace_id, scope.venture_id)
                or row.fallback or row.published):
            return
        validate_binding(row.workspace_id, row.venture_id)
        material = _parse_json(row.copy_json)
        snapshot = material.get("_render_snapshot")
        html = snapshot.get("html") if isinstance(snapshot, dict) else None
        if (not isinstance(html, str) or len(html.encode()) > 2_000_000
                or not html.strip() or hashlib.sha256(html.encode()).hexdigest() != row.sha256
                or copy_hash(material) != row.copy_hash):
            return
        # Evidence performs a second scoped fresh read. Exact JSON freezes the
        # durable render snapshot even if ephemeral ECS files disappear.
        persisted_record(SiteVersion, row.id, {key: getattr(row, key) for key in (
            "venture_id", "workspace_id", "sha256", "copy_hash", "copy_json",
            "brandkit_hash", "brandkit_json", "path", "version", "fallback", "published")})


def get_latest(workspace_id: str, *, venture_id: str | None = None) -> SiteVersionView | None:
    factory = db()
    with factory() as session:
        row = session.scalars(
            select(SiteVersion)
            .where(SiteVersion.workspace_id == workspace_id)
            .where(SiteVersion.venture_id == venture_id if venture_id is not None else True)
            .order_by(SiteVersion.version.desc())
            .limit(1)
        ).first()
        if row is None:
            return None
        session.expunge(row)
    return view(row)


def get_latest_published(workspace_id: str) -> SiteVersionView | None:
    with db()() as session:
        row = session.scalars(
            select(SiteVersion).where(SiteVersion.workspace_id == workspace_id, SiteVersion.published == 1)
            .order_by(SiteVersion.version.desc()).limit(1)
        ).first()
        return view(row) if row is not None else None


def get_by_version(workspace_id: str, version: int, *, venture_id: str | None = None) -> SiteVersionView | None:
    factory = db()
    with factory() as session:
        row = session.scalars(
            select(SiteVersion).where(
                SiteVersion.workspace_id == workspace_id,
                SiteVersion.version == version,
                SiteVersion.venture_id == venture_id if venture_id is not None else True,
            )
        ).first()
        if row is None:
            return None
        session.expunge(row)
    return view(row)


def patch_copy(workspace_id: str, version: int, copy: dict[str, Any]) -> None:
    """Keep rendered HTML in Postgres so an ECS roll does not lose the page."""
    factory = db()
    with factory() as session:
        row = session.scalars(
            select(SiteVersion).where(
                SiteVersion.workspace_id == workspace_id,
                SiteVersion.version == version,
            )
        ).first()
        if row is None:
            return
        row.copy_json = json.dumps(copy, sort_keys=True)
        session.commit()


__all__ = [
    "MAX_VARIANTS",
    "SiteVersionView",
    "get_by_version",
    "get_latest",
    "list_versions",
    "next_version",
    "patch_copy",
    "persist",
]
