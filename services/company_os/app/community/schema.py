"""Idempotent community tables for create_all / SQLite local DBs."""

from __future__ import annotations

from sqlalchemy.engine import Engine

from app.db.models import Base

_TABLES = (
    "community_plans",
    "community_servers",
    "community_people",
    "community_lab_logs",
)


def ensure_community_schema(engine: Engine) -> None:
    wanted = [Base.metadata.tables[name] for name in _TABLES if name in Base.metadata.tables]
    if wanted:
        Base.metadata.create_all(engine, tables=wanted)
