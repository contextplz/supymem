"""QM client env settings — mirrors ops/config.py style (doc 55 §7)."""

from __future__ import annotations

import os


def qm_base_url() -> str:
    return (os.environ.get("QM_BASE_URL") or "http://127.0.0.1:3500").rstrip("/")


def qm_source_signing_secret() -> str:
    return (os.environ.get("QM_SOURCE_SIGNING_SECRET") or "").strip()


def qm_ops_turn_timeout_s() -> float:
    raw = (os.environ.get("QM_OPS_TURN_TIMEOUT_S") or "120").strip()
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 120.0


def qm_build_turn_timeout_s() -> float:
    raw = (os.environ.get("QM_BUILD_TURN_TIMEOUT_S") or "900").strip()
    try:
        return max(1.0, float(raw))
    except ValueError:
        return 900.0


# Stable local-founder id from app.workspaces.resolve — duplicated here so the
# QM client can be imported without pulling the SQLAlchemy ORM stack.
_DEFAULT_PERSON_ID = "per_local000000"


def qm_actor_external_id(person_id: str | None = None) -> str:
    """Stable QM ActorAssertion.externalId for a Company OS person (doc 55 §3)."""
    pid = (person_id or _DEFAULT_PERSON_ID).strip()
    return f"companyos:{pid}"
