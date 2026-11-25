"""Founder App lifecycle with a local adapter (doc 15). AWS apply is doc 37 H-11."""

from __future__ import annotations

import json
import uuid
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.builder.ctl import _ensure_app
from app.config import data_root
from app.core.fsm import advance as fsm_transition
from app.db.supymem_models import AppAuthPool, AppDatabase, FounderApp
from app.state import db
from core.clock import utcnow
from core.ids import new_id


def create_draft(*, workspace_id: str, venture_id: str, name: str, slug: str,
                 kind: str = "service", build_mode: str = "supymem", deploy_target: str = "supymem",
                 actor: str, idempotency_key: str | None = None) -> dict[str, Any]:
    import hashlib
    from sqlalchemy.exc import IntegrityError
    from app.builder.access import authorize
    from app.db.session import serialize_writes
    from app.db.supymem_models import FounderRequest

    payload = {"name": name, "slug": slug, "kind": kind, "build_mode": build_mode, "deploy_target": deploy_target}
    # Legacy clients without a header still retry the same named draft safely.
    intent = idempotency_key or "draft-slug:" + slug
    if not 1 <= len(intent) <= 200:
        raise ValueError("app_idempotency_key_invalid")
    request_id = "frq_" + hashlib.sha256((workspace_id + ":" + venture_id + ":" + actor + ":app:" + intent).encode()).hexdigest()[:32]
    now = utcnow()
    with db()() as session:
        serialize_writes(session, key=request_id)
        authorize(session, workspace_id=workspace_id, venture_id=venture_id, actor=actor, mutate=True)
        prior = session.get(FounderRequest, request_id)
        if prior:
            saved = json.loads(prior.text)
            if saved["request"] != payload:
                raise ValueError("app_idempotency_conflict")
            row = session.get(FounderApp, prior.related_id)
            if row is None or row.deleted_at:
                raise ValueError("app_intent_no_longer_active")
            return _serialize(row)
        row = FounderApp(id=new_id("app"), workspace_id=workspace_id, venture_id=venture_id,
                         name=name, slug=slug, kind=kind, state="draft", build_mode=build_mode,
                         deploy_target=deploy_target, created_at=now, updated_at=now)
        session.add(row)
        session.add(FounderRequest(id=request_id, workspace_id=workspace_id, venture_id=venture_id,
            source="app_create", text=json.dumps({"request": payload, "actor": actor}, sort_keys=True),
            classification="founder_approved", outcome="applied", related_id=row.id,
            created_at=now, resolved_at=now))
        try:
            session.commit()
        except IntegrityError as exc:
            raise ValueError("slug_taken") from exc
        return _serialize(row)


def provision(payload: dict[str, Any]) -> dict[str, Any]:
    from app.worker.handlers import require_capability
    require_capability("infra.provision")
    factory = db()
    now = utcnow()
    with factory() as session:
        app = _ensure_app(session, payload["workspace_id"], payload["venture_id"])
        if app.state == "draft":
            fsm_transition(session, app, "provisioning")
        db_name = f"app_{app.id[-12:]}"
        existing_db = session.scalar(select(AppDatabase).where(AppDatabase.app_id == app.id))
        if existing_db is None:
            existing_db = AppDatabase(
                id=new_id("adb"),
                app_id=app.id,
                workspace_id=app.workspace_id,
                engine="postgres",
                placement="shared",
                db_name=db_name,
                role_name=f"{db_name}_role",
                state="ready",
                created_at=now,
            )
            session.add(existing_db)
        pool = session.scalar(select(AppAuthPool).where(AppAuthPool.app_id == app.id))
        if pool is None:
            pool = AppAuthPool(
                id=new_id("aap"),
                app_id=app.id,
                workspace_id=app.workspace_id,
                provider="local",
                pool_id=f"local-{app.id}",
                client_id=f"local-client-{app.id}",
                domain_prefix=app.slug,
                hosted_ui_url=None,
                state="ready",
                config=json.dumps({"adapter": "local"}),
                created_at=now,
            )
            session.add(pool)
        meta = {
            "cognito_pool": pool.pool_id,
            "task_role": f"local-task-{app.id}",
            "sg": f"local-sg-{app.id}",
            "adapter": "local",
        }
        root = data_root() / "apps" / app.id
        root.mkdir(parents=True, exist_ok=True)
        (root / "infra.json").write_text(json.dumps(meta, indent=2), encoding="utf-8")
        if app.state == "provisioning":
            fsm_transition(session, app, "building")
        if app.state == "building":
            fsm_transition(session, app, "deployed")
        if app.state == "deployed":
            fsm_transition(session, app, "live")
        app.updated_at = now
        session.commit()
        return {"app_id": app.id, "infra": meta, "database": existing_db.db_name, "auth_pool": pool.pool_id}


def sleep_app(app_id: str) -> dict[str, Any]:
    from app.worker.handlers import require_capability
    require_capability("infra.sleep_idle")
    factory = db()
    with factory() as session:
        app = session.get(FounderApp, app_id)
        if app is None:
            raise KeyError(app_id)
        if app.state == "live":
            fsm_transition(session, app, "sleeping")
        app.updated_at = utcnow()
        session.commit()
        return {"id": app.id, "state": app.state}


def wake_app(app_id: str) -> dict[str, Any]:
    from app.worker.handlers import require_capability
    require_capability("infra.provision")
    factory = db()
    with factory() as session:
        app = session.get(FounderApp, app_id)
        if app is None:
            raise KeyError(app_id)
        if app.state == "sleeping":
            fsm_transition(session, app, "live")
        app.updated_at = utcnow()
        session.commit()
        return {"id": app.id, "state": app.state}


def list_apps(venture_id: str) -> list[dict[str, Any]]:
    factory = db()
    with factory() as session:
        rows = list(
            session.scalars(
                select(FounderApp).where(
                    FounderApp.venture_id == venture_id,
                    FounderApp.deleted_at.is_(None),
                    FounderApp.kind != "local_hermes",
                ).order_by(FounderApp.created_at.asc(), FounderApp.id.asc())
            )
        )
        return [_serialize(a) for a in rows]


def get_app(app_id: str) -> dict[str, Any]:
    factory = db()
    with factory() as session:
        app = session.get(FounderApp, app_id)
        if app is None or app.deleted_at is not None:
            raise KeyError(app_id)
        return _serialize(app)


def slug_available(slug: str) -> dict[str, Any]:
    factory = db()
    with factory() as session:
        hit = session.scalar(select(FounderApp).where(FounderApp.slug == slug))
        return {"available": hit is None, "reason": None if hit is None else "taken"}


def _alias_host(session: Session, slug: str) -> bool:
    """A retired address still serves its old page, so nobody else may claim it."""
    from app.db.supymem_models import AppDomain

    return session.scalar(select(AppDomain.id).where(AppDomain.hostname.like(slug + ".%"))) is not None


def free_slug(base: str, *, session: Session | None = None) -> str:
    """First unused address for `base`: base, base-2, base-3, ... (addresses are global across founders)."""
    for n in range(1, 100):
        suffix = "" if n == 1 else f"-{n}"
        candidate = base[: 63 - len(suffix)].strip("-") + suffix
        available = (
            session.scalar(select(FounderApp.id).where(FounderApp.slug == candidate)) is None
            and not _alias_host(session, candidate)
            if session is not None else slug_available(candidate).get("available")
        )
        if available:
            return candidate
    return f"{base[:54].strip('-')}-{uuid.uuid4().hex[:8]}"


def _serialize(app: FounderApp) -> dict[str, Any]:
    return {
        "id": app.id,
        "venture_id": app.venture_id,
        "workspace_id": app.workspace_id,
        "slug": app.slug,
        "name": app.name,
        "kind": app.kind,
        "state": app.state,
        "build_mode": app.build_mode,
        "deploy_target": app.deploy_target,
    }


def reconcile(_payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Local adapter: report apps whose infra.json is missing."""
    factory = db()
    orphans: list[str] = []
    with factory() as session:
        apps = list(session.scalars(select(FounderApp).where(FounderApp.deleted_at.is_(None))))
        for app in apps:
            path = data_root() / "apps" / app.id / "infra.json"
            if app.state in {"live", "sleeping", "deployed"} and not path.is_file():
                orphans.append(app.id)
    return {"orphans": orphans, "adapter": "local"}

from fastapi import Header, HTTPException
from app.authz.workspace import assert_venture_access
from app.db.models import Venture
from app.db.supymem_models import BuildRun, QaRun
from app.landing.service import generate_and_publish

def apps_app_or_404(app_id: str) -> FounderApp:
    factory = db()
    with factory() as session:
        app = session.get(FounderApp, app_id)
        if app is None or app.deleted_at is not None:
            raise HTTPException(404, detail={"error": {"code": "not_found", "message": "app"}})
        session.expunge(app)
        return app

def apps_qa_list(
    app_id: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    app = apps_app_or_404(app_id)
    assert_venture_access(app.venture_id, x_subject)
    factory = db()
    with factory() as session:
        rows = list(session.scalars(select(QaRun).where(QaRun.app_id == app_id,
            QaRun.workspace_id == app.workspace_id, QaRun.venture_id == app.venture_id).limit(100)))
        from app.builder.qa_receipts import serialize
        items = []
        for row in rows:
            run = session.get(BuildRun, row.build_run_id)
            if run and (run.app_id, run.workspace_id, run.venture_id) == (app.id, app.workspace_id, app.venture_id):
                items.append(serialize(row))
        return {"items": items}

def apps_publish_version(
    app_id: str,
    version: str,
    x_subject: str | None = Header(default=None, alias="X-Subject"),
):
    app = apps_app_or_404(app_id)
    assert_venture_access(app.venture_id, x_subject)
    factory = db()
    with factory() as session:
        ven = session.get(Venture, app.venture_id)
        idea = ven.raw_idea if ven else ""
    return generate_and_publish(app.venture_id, app.workspace_id, idea or "")
