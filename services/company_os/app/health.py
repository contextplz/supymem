"""Liveness and readiness for Company OS core.

Hard: database connectivity and alembic head. Without either the process must
not receive traffic.
Soft: warden (governance calls fail but reads still work) and LLM (copy and
chat degrade to offline/fallback). Never probe the LLM network from /ready.
Local readiness checks only the database and schema; /local/health reports your
optional connections separately.
"""

from __future__ import annotations

import json
import logging
import os
from functools import lru_cache
from pathlib import Path

import httpx
from fastapi.responses import JSONResponse
from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.pool import NullPool

from core.env_contract import parse_app_env
from core.health import Check, liveness_payload, readiness_payload

from app.config import settings
from app.db.session import resolve_database_url

log = logging.getLogger(__name__)

SERVICE = "company_os"
_PROBE_TIMEOUT_S = 1.0
_WARDEN_TIMEOUT_S = 0.5


def _sync_db_url(url: str) -> str:
    if url.startswith("sqlite+aiosqlite://"):
        return "sqlite://" + url.removeprefix("sqlite+aiosqlite://")
    if url.startswith("postgresql+asyncpg://"):
        return "postgresql://" + url.removeprefix("postgresql+asyncpg://")
    return url


def _connect_args(url: str) -> dict[str, object]:
    if url.startswith("sqlite"):
        return {"timeout": _PROBE_TIMEOUT_S, "check_same_thread": False}
    return {"connect_timeout": int(_PROBE_TIMEOUT_S) or 1}


def _db_url() -> str:
    return resolve_database_url() or settings.db_url


@lru_cache(maxsize=1)
def expected_schema_revision() -> str | None:
    """Alembic head revision. ``None`` means the script directory could not be read."""
    try:
        from alembic.config import Config
        from alembic.script import ScriptDirectory

        # health.py lives in the `app` package; migrations sit next to it.
        # Do not walk up to alembic.ini — that path is wrong if this module is
        # imported from site-packages instead of the service checkout.
        app_dir = Path(__file__).resolve().parent
        cfg = Config()
        cfg.set_main_option("script_location", str(app_dir / "db" / "migrations"))
        script = ScriptDirectory.from_config(cfg)
        heads = script.get_heads()
        if len(heads) != 1:
            return None
        return heads[0]
    except Exception:  # noqa: BLE001 — probe must never raise
        log.exception("could not read alembic heads")
        return None


def _probe_database() -> tuple[Check, Check]:
    url = _sync_db_url(_db_url())
    expected = expected_schema_revision()
    engine = None
    current: str | None = None
    try:
        engine = create_engine(
            url,
            poolclass=NullPool,
            connect_args=_connect_args(url),
        )
        with engine.connect() as conn:
            conn.execute(text("SELECT 1"))
            try:
                current = conn.execute(
                    text("SELECT version_num FROM alembic_version")
                ).scalar()
            except SQLAlchemyError:
                current = None
    except Exception:  # noqa: BLE001 — dialect missing, refused, timeout
        return (
            Check("database", False, True, "connection failed"),
            Check("schema", False, True, "not checked"),
        )
    finally:
        if engine is not None:
            engine.dispose()
    db_ok = Check("database", True, True, "ok")
    if expected is None:
        return db_ok, Check("schema", False, True, "alembic heads unreadable")
    if current is None:
        return db_ok, Check("schema", False, True, "alembic_version missing")
    if str(current) != expected:
        return db_ok, Check("schema", False, True, "not at expected revision")
    return db_ok, Check("schema", True, True, "ok")


def _probe_warden() -> Check:
    base = (settings.warden_url or "").rstrip("/")
    if not base:
        return Check("warden", False, False, "unconfigured")
    try:
        with httpx.Client(timeout=_WARDEN_TIMEOUT_S) as client:
            r = client.get(f"{base}/health")
        if r.status_code == 200:
            return Check("warden", True, False, "ok")
        return Check("warden", False, False, "unhealthy")
    except Exception:  # noqa: BLE001 — never hang /ready
        return Check("warden", False, False, "unreachable")


def _probe_llm() -> Check:
    offline = os.environ.get("LLM_OFFLINE", "true").lower() in {"1", "true", "yes"}
    if offline:
        return Check("llm", True, False, "offline")
    if (os.environ.get("LLM_API_KEY") or "").strip():
        return Check("llm", True, False, "configured")
    return Check("llm", False, False, "unconfigured")


def health_response() -> dict[str, str]:
    return liveness_payload(SERVICE)


def search_diagnostics(owner_id: str) -> dict:
    from sqlalchemy import select
    from app.authz.workspace import accessible_workspace_ids
    from app.db.models import Job
    from app.state import db

    workspaces = accessible_workspace_ids(owner_id)
    with db()() as session:
        row = session.scalar(select(Job).where(Job.kind == "gtm.journey.prepare",
            Job.workspace_id.in_(workspaces), Job.payload_json.contains('"search_queries": {'))
            .order_by(Job.created_at.desc(), Job.id.desc()).limit(1))
        if not row:
            return {"job_id": None, "diagnostics": {},
                "next_step": "Run Find customers, then check these diagnostics again."}
        runtime = json.loads(row.payload_json).get("_runtime", {})
        return {"job_id": row.id, "diagnostics": {key: runtime.get(key, {})
            for key in ("source_yield", "search_queries")}}


def ready_response() -> JSONResponse:
    from app.core.local_mode import is_local

    try:
        app_env = parse_app_env()
    except Exception:  # noqa: BLE001
        app_env = "invalid"
    db_check, schema_check = _probe_database()
    checks = [db_check, schema_check]
    if not is_local():
        checks.extend((_probe_warden(), _probe_llm()))
    body, status = readiness_payload(SERVICE, checks, app_env=app_env)
    return JSONResponse(body, status_code=status)
